"""
ConvoBooks Data Reset Management Command
=========================================
Usage:
  python manage.py reset_data --dry-run   # Show record counts only (safe)
  python manage.py reset_data --execute   # Execute the reset (DESTRUCTIVE)

The --execute flag requires explicit confirmation at the prompt.
"""
import sys
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.contrib.auth import get_user_model


# ---------------------------------------------------------------------------
# Ordered deletion plan
# ---------------------------------------------------------------------------
# Tables are deleted leaf-first (children before parents) to satisfy FK
# constraints.  Django CASCADE would handle most of this, but explicit ordering
# avoids surprises and respects PROTECT relationships.
#
# Format: (model_label, django_app.ModelClass string, preserve_filter_or_None)
#   preserve_filter: a Q expression string to pass to .exclude() – set None to
#                    delete all rows.
#
# IMPORTANT:
#   - AuditLog.delete() raises ProtectedError → we bypass via raw SQL TRUNCATE.
#   - django.contrib.auth.User rows that belong to superusers with no profile
#     (platform-level) are PRESERVED.
#   - Sequence rows reset to 0 so IDs restart.
# ---------------------------------------------------------------------------

RESET_PLAN = [
    # ── DPDP Governance ───────────────────────────────────────────────────
    ("DPDPBreachIncident",          "books.DPDPBreachIncident"),
    ("DPDPDataSubjectRequest",      "books.DPDPDataSubjectRequest"),
    ("DPDPConsentRecord",           "books.DPDPConsentRecord"),
    ("DPDPDataProcessor",           "books.DPDPDataProcessor"),

    # ── Reliability / async infra ─────────────────────────────────────────
    ("PostingOutbox",               "books.PostingOutbox"),
    ("ProcessingJob",               "books.ProcessingJob"),
    ("IdempotencyRecord",           "books.IdempotencyRecord"),

    # ── Fixed assets ──────────────────────────────────────────────────────
    ("DepreciationEntry",           "books.DepreciationEntry"),
    ("FixedAsset",                  "books.FixedAsset"),

    # ── Budgets ───────────────────────────────────────────────────────────
    ("Budget",                      "books.Budget"),

    # ── Exchange rates ────────────────────────────────────────────────────
    ("ExchangeRate",                "books.ExchangeRate"),

    # ── Mileage claims ────────────────────────────────────────────────────
    ("MileageExpenseClaim",         "books.MileageExpenseClaim"),

    # ── Balance netting ───────────────────────────────────────────────────
    ("BalanceNetting",              "books.BalanceNetting"),

    # ── Bill of Entry ─────────────────────────────────────────────────────
    ("BillOfEntry",                 "books.BillOfEntry"),

    # ── GSTR returns ──────────────────────────────────────────────────────
    ("GSTRReturn",                  "books.GSTRReturn"),

    # ── Subscriptions ─────────────────────────────────────────────────────
    ("SubscriptionInvoice",         "books.SubscriptionInvoice"),
    ("TenantSubscription",          "books.TenantSubscription"),

    # ── Developer / webhook ───────────────────────────────────────────────
    ("WebhookSubscription",         "books.WebhookSubscription"),
    ("DeveloperApiKey",             "books.DeveloperApiKey"),
    ("WebhookConfig",               "books.WebhookConfig"),

    # ── Background & payment transactions ─────────────────────────────────
    ("BackgroundTask",              "books.BackgroundTask"),
    ("SecurityBackupRecord",        "books.SecurityBackupRecord"),
    ("PaymentTransaction",          "books.PaymentTransaction"),

    # ── Notifications ─────────────────────────────────────────────────────
    ("Notification",                "books.Notification"),

    # ── Portal access ─────────────────────────────────────────────────────
    ("VendorPortalAccess",          "books.VendorPortalAccess"),
    ("CustomerPortalAccess",        "books.CustomerPortalAccess"),

    # ── Anomalies ─────────────────────────────────────────────────────────
    ("FinancialAnomaly",            "books.FinancialAnomaly"),

    # ── Bank ──────────────────────────────────────────────────────────────
    ("BankReconciliation",          "books.BankReconciliation"),
    ("BankRule",                    "books.BankRule"),
    ("BankDeposit",                 "books.BankDeposit"),
    ("BankTransfer",                "books.BankTransfer"),
    ("BankTransaction",             "books.BankTransaction"),
    ("BankStatementBatch",          "books.BankStatementBatch"),
    ("BankFeedConnection",          "books.BankFeedConnection"),

    # ── Documents – attachments ───────────────────────────────────────────
    ("Attachment",                  "books.Attachment"),
    ("ManagedDocument",             "books.ManagedDocument"),
    ("DocumentFolder",              "books.DocumentFolder"),

    # ── e-Invoice / e-Way ─────────────────────────────────────────────────
    ("EInvoiceRecord",              "books.EInvoiceRecord"),
    ("EWayBill",                    "books.EWayBill"),

    # ── Projects / time ───────────────────────────────────────────────────
    ("TimeLog",                     "books.TimeLog"),
    ("ProjectTaskAssignment",       "books.ProjectTaskAssignment"),
    ("ProjectTask",                 "books.ProjectTask"),
    ("ProjectMilestone",            "books.ProjectMilestone"),
    ("ProjectStaff",                "books.ProjectStaff"),
    ("Project",                     "books.Project"),

    # ── Stock ─────────────────────────────────────────────────────────────
    ("StockMovement",               "books.StockMovement"),
    ("StockTransferLine",           "books.StockTransferLine"),

    # ── Delivery challan ──────────────────────────────────────────────────
    ("DeliveryChallanLine",         "books.DeliveryChallanLine"),
    ("DeliveryChallan",             "books.DeliveryChallan"),

    # ── Credit / vendor credit ────────────────────────────────────────────
    ("CreditNoteLine",              "books.CreditNoteLine"),
    ("CreditNote",                  "books.CreditNote"),
    ("VendorCreditLine",            "books.VendorCreditLine"),
    ("VendorCredit",                "books.VendorCredit"),

    # ── Expenses ──────────────────────────────────────────────────────────
    ("Expense",                     "books.Expense"),

    # ── Recurring ─────────────────────────────────────────────────────────
    ("RecurringExpense",            "books.RecurringExpense"),
    ("RecurringBill",               "books.RecurringBill"),

    # ── Retainer ──────────────────────────────────────────────────────────
    ("RetainerInvoiceAllocation",   "books.RetainerInvoiceAllocation"),
    ("RetainerPayment",             "books.RetainerPayment"),
    ("RetainerInvoiceLine",         "books.RetainerInvoiceLine"),
    ("RetainerInvoice",             "books.RetainerInvoice"),

    # ── Purchase ──────────────────────────────────────────────────────────
    ("BillAllocation",              "books.BillAllocation"),
    ("BillLine",                    "books.BillLine"),
    ("PurchaseOrderLine",           "books.PurchaseOrderLine"),
    ("PurchaseOrder",               "books.PurchaseOrder"),
    ("Bill",                        "books.Bill"),

    # ── Payments made ─────────────────────────────────────────────────────
    ("PaymentMade",                 "books.PaymentMade"),

    # ── Sales ─────────────────────────────────────────────────────────────
    ("ReceivedAllocation",          "books.ReceivedAllocation"),
    ("PaymentReceived",             "books.PaymentReceived"),
    ("SalesOrderLine",              "books.SalesOrderLine"),
    ("SalesOrder",                  "books.SalesOrder"),
    ("QuoteLine",                   "books.QuoteLine"),
    ("Quote",                       "books.Quote"),

    # ── Recurring invoice ─────────────────────────────────────────────────
    ("RecurringInvoice",            "books.RecurringInvoice"),

    # ── Invoice lines (before invoices) ───────────────────────────────────
    ("InvoiceLine",                 "books.InvoiceLine"),
    ("Invoice",                     "books.Invoice"),

    # ── Stock transfers (delete() raises ProtectedError → use raw SQL) ────
    # Handled separately below in raw-SQL section.

    # ── Journal ───────────────────────────────────────────────────────────
    ("JournalItem",                 "books.JournalItem"),
    ("Journal",                     "books.Journal"),
    ("LockDate",                    "books.LockDate"),

    # ── Chart of accounts ─────────────────────────────────────────────────
    ("CustomAccountView",           "books.CustomAccountView"),
    # Account deletion deferred – handled after JournalItems

    # ── Bank accounts (after transactions) ────────────────────────────────
    ("BankAccount",                 "books.BankAccount"),

    # ── Inventory / items ─────────────────────────────────────────────────
    ("Warehouse",                   "books.Warehouse"),
    ("Item",                        "books.Item"),
    ("ItemCategory",                "books.ItemCategory"),
    ("PriceList",                   "books.PriceList"),

    # ── Customers / vendors ───────────────────────────────────────────────
    ("Customer",                    "books.Customer"),
    ("Vendor",                      "books.Vendor"),

    # ── Org-level config ──────────────────────────────────────────────────
    ("Salesperson",                 "books.Salesperson"),
    ("CurrencySetting",             "books.CurrencySetting"),
    ("PaymentTermSetting",          "books.PaymentTermSetting"),
    ("CustomField",                 "books.CustomField"),
    ("WorkflowRule",                "books.WorkflowRule"),

    # ── Document numbering ────────────────────────────────────────────────
    ("DocumentNumberAllocation",    "books.DocumentNumberAllocation"),
    ("DocumentNumberSeriesCounter", "books.DocumentNumberSeriesCounter"),
    ("DocumentNumberSeries",        "books.DocumentNumberSeries"),
    ("TransactionSeries",           "books.TransactionSeries"),
    ("DocumentNumberPreference",    "books.DocumentNumberPreference"),

    # ── Users / invitations (non-superuser) ───────────────────────────────
    ("OTPVerification",             "books.OTPVerification"),
    ("UserInvitation",              "books.UserInvitation"),
    ("OrganizationMembership",      "books.OrganizationMembership"),
    # UserProfile and User are handled specially below

    # ── Roles ─────────────────────────────────────────────────────────────
    ("Role",                        "books.Role"),

    # ── Branch / warehouse (after child documents) ────────────────────────
    ("Branch",                      "books.Branch"),

    # ── Organization (last) ───────────────────────────────────────────────
    ("Organization",                "books.Organization"),

    # ── Sequences ─────────────────────────────────────────────────────────
    ("Sequence",                    "books.Sequence"),
]

# Tables we bypass Django's ORM delete() for (ProtectedError / immutable):
RAW_TRUNCATE_TABLES = [
    "books_audiolog",           # AuditLog.delete() raises ProtectedError
    "books_stocktransferline",  # Must go before stock_transfer
    "books_stocktransfer",      # StockTransfer.delete() raises ProtectedError
    "books_account",            # Account has PROTECT children; truncate after clearing refs
]

# Django auth tables to selectively purge (non-superuser only):
AUTH_TABLES = [
    "auth_user_user_permissions",
    "auth_user_groups",
]


def get_model(label):
    from django.apps import apps
    app, name = label.split(".")
    return apps.get_model(app, name)


class Command(BaseCommand):
    help = "ConvoBooks complete data reset. Use --dry-run to preview, --execute to run."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Only print record counts; do not delete anything.")
        parser.add_argument("--execute", action="store_true",
                            help="Actually perform the deletion after confirmation.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        execute = options["execute"]

        if not dry_run and not execute:
            self.stderr.write(self.style.ERROR(
                "Specify either --dry-run (safe preview) or --execute (destructive reset)."
            ))
            sys.exit(1)

        self.stdout.write(self.style.WARNING(
            "\n" + "=" * 70 + "\n"
            "  ConvoBooks Data Reset Tool\n"
            "  Mode: " + ("DRY RUN (no changes)" if dry_run else "EXECUTE (DESTRUCTIVE)") + "\n"
            + "=" * 70
        ))

        # ── Collect record counts ──────────────────────────────────────────
        counts = {}
        total = 0

        self.stdout.write("\n📊 Pre-Reset Record Counts\n" + "-" * 50)

        for label, model_path in RESET_PLAN:
            try:
                mdl = get_model(model_path)
                cnt = mdl.objects.count()
                counts[label] = cnt
                total += cnt
                mark = "⚠️ " if cnt > 0 else "✅"
                self.stdout.write(f"  {mark} {label:<35} {cnt:>8,}")
            except Exception as exc:
                self.stdout.write(f"  ❓ {label:<35} ERROR: {exc}")
                counts[label] = -1

        # AuditLog (raw – immutable)
        with connection.cursor() as cur:
            for tbl in ["books_audiolog", "books_stocktransfer", "books_stocktransferline"]:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    cnt = cur.fetchone()[0]
                    counts[tbl] = cnt
                    total += cnt
                    mark = "⚠️ " if cnt > 0 else "✅"
                    self.stdout.write(f"  {mark} {tbl:<35} {cnt:>8,}  [raw]")
                except Exception as exc:
                    self.stdout.write(f"  ❓ {tbl:<35} ERROR: {exc}")

        # Auth users (non-superuser)
        User = get_user_model()
        user_cnt = User.objects.filter(is_superuser=False).count()
        su_cnt = User.objects.filter(is_superuser=True).count()
        self.stdout.write(f"\n  Users (non-superuser, to delete): {user_cnt:>6,}")
        self.stdout.write(f"  Users (superuser, PRESERVED):     {su_cnt:>6,}")

        # Django sessions
        with connection.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) FROM django_session")
                sess_cnt = cur.fetchone()[0]
                self.stdout.write(f"  Django sessions (to delete):       {sess_cnt:>6,}")
            except Exception:
                sess_cnt = 0

        self.stdout.write(f"\n  {'TOTAL BUSINESS RECORDS':35} {total:>8,}")
        self.stdout.write("-" * 50)

        # ── Preservation summary ───────────────────────────────────────────
        self.stdout.write(
            "\n🔒 Components that will be PRESERVED:\n"
            "   • Django schema and all migrations\n"
            "   • Superuser accounts\n"
            "   • Django content-types and permissions (system tables)\n"
            "   • Source code, Dockerfile, buildspec.yml, AWS config\n"
            "   • Redis / Celery configuration\n"
        )

        self.stdout.write(
            "\n🗑️  Data that will be DELETED:\n"
            "   • All organizations and their business records\n"
            "   • All customers, vendors, items, inventory\n"
            "   • All invoices, bills, quotes, sales orders\n"
            "   • All payments, bank accounts, bank transactions\n"
            "   • All journal entries and chart-of-accounts data\n"
            "   • All users (non-superuser) and their profiles\n"
            "   • All audit logs (immutable table truncated via raw SQL)\n"
            "   • All stock transfers (ProtectedError table, raw SQL)\n"
            "   • All uploaded file records (Attachment, ManagedDocument)\n"
            "   • All sequences (auto-increment counters reset)\n"
            "   • All Django sessions\n"
        )

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                "\n✅ DRY RUN complete. No data was changed.\n"
                "   Run with --execute to perform the actual reset.\n"
            ))
            return

        # ── Confirmation gate ──────────────────────────────────────────────
        self.stdout.write(self.style.ERROR(
            "\n⚠️  WARNING: This is IRREVERSIBLE. All business data will be deleted.\n"
        ))
        confirm = input("Type RESET to confirm, or anything else to abort: ").strip()
        if confirm != "RESET":
            self.stdout.write(self.style.WARNING("Aborted. No data was deleted."))
            return

        # ── Execute deletion ───────────────────────────────────────────────
        self.stdout.write("\n🔄 Starting reset...\n")
        deleted_counts = {}

        with transaction.atomic():
            # 1. Truncate immutable / ProtectedError tables via raw SQL
            self.stdout.write("  → Truncating immutable / protected tables via raw SQL...")
            with connection.cursor() as cur:
                for tbl in RAW_TRUNCATE_TABLES:
                    try:
                        cur.execute(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
                        deleted_counts[tbl] = counts.get(tbl, "?")
                        self.stdout.write(f"    ✅ TRUNCATED {tbl}")
                    except Exception as exc:
                        self.stdout.write(f"    ⚠️  {tbl}: {exc}")

            # 2. ORM deletion in dependency order
            for label, model_path in RESET_PLAN:
                if label in ("Sequence",):
                    # Reset sequences to 0 instead of deleting
                    continue
                try:
                    mdl = get_model(model_path)
                    n, _ = mdl.objects.all().delete()
                    deleted_counts[label] = n
                    if n > 0:
                        self.stdout.write(f"    ✅ Deleted {n:,} {label}")
                    else:
                        self.stdout.write(f"    ○  {label}: 0 rows")
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"    ⚠️  {label}: {exc}"))

            # 3. Reset Sequence counters
            from books.models import Sequence
            Sequence.objects.all().update(value=0)
            self.stdout.write("    ✅ Sequence counters reset to 0")

            # 4. Delete non-superuser auth users (cascades UserProfile via OneToOne)
            from books.models import UserProfile
            self.stdout.write("  → Deleting non-superuser users and profiles...")
            User = get_user_model()
            n_profiles, _ = UserProfile.objects.all().delete()
            n_users, _ = User.objects.filter(is_superuser=False).delete()
            self.stdout.write(f"    ✅ Deleted {n_profiles:,} UserProfile rows")
            self.stdout.write(f"    ✅ Deleted {n_users:,} User rows (non-superuser)")

            # 5. Clear Django sessions
            with connection.cursor() as cur:
                cur.execute("DELETE FROM django_session")
                self.stdout.write("    ✅ Cleared django_session")

            # 6. Reset PostgreSQL sequences for all books_ tables
            self.stdout.write("  → Resetting PostgreSQL auto-increment sequences...")
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT sequence_name
                    FROM information_schema.sequences
                    WHERE sequence_schema = 'public'
                    AND sequence_name LIKE 'books_%'
                """)
                seqs = [row[0] for row in cur.fetchall()]
                for seq in seqs:
                    try:
                        cur.execute(f"ALTER SEQUENCE {seq} RESTART WITH 1")
                    except Exception:
                        pass
            self.stdout.write(f"    ✅ Reset {len(seqs)} PostgreSQL sequences")

        self.stdout.write(self.style.SUCCESS(
            "\n✅ Data reset complete!\n"
            "   The application database is now empty.\n"
            "   You can register a new organization to start fresh.\n"
        ))
