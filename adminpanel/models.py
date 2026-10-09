from django.conf import settings
from django.db import models


class AdminUserRole(models.Model):
    ROLE_OWNER = "owner"
    ROLE_OPERATIONS = "operations"
    ROLE_AUDITOR = "auditor"

    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_OPERATIONS, "Operations Manager"),
        (ROLE_AUDITOR, "Auditor / Viewer"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_role")
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default=ROLE_OPERATIONS)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_user_role"

    def __str__(self):
        return f"{self.user_id} - {self.role}"


class AdminUserAccessPolicy(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_access_policy")
    access_rules = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_user_access_policy"

    def __str__(self):
        return f"{self.user_id} access policy"


class ContactMessage(models.Model):
    """A message sent through the public "Get in touch" form on the customer page.

    Stored here so the top-level (owner) admin can read and follow up on every
    message from the admin app's "Messages" page.
    """

    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"

    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    SOURCE_CUSTOMER = "customer"
    SOURCE_MERCHANT = "merchant"
    SOURCE_CHOICES = [
        (SOURCE_CUSTOMER, "Customer"),
        (SOURCE_MERCHANT, "Merchant"),
    ]

    # Which public form the message came from: the customer page ("Get in touch")
    # or the merchant site's landing page ("Still have a question?").
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_CUSTOMER, db_index=True)
    # Topic picked on the merchant form (empty for customer messages).
    topic = models.CharField(max_length=80, blank=True)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    order_id = models.CharField(max_length=64, blank=True)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    internal_note = models.TextField(blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_contact_message"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.status})"


class ContactReply(models.Model):
    """An email reply an admin sent to a ContactMessage from the admin app."""

    message = models.ForeignKey(ContactMessage, on_delete=models.CASCADE, related_name="replies")
    subject = models.CharField(max_length=200)
    body = models.TextField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_contact_reply"
        ordering = ["-sent_at", "-id"]

    def __str__(self):
        return f"Reply to message {self.message_id} at {self.sent_at}"


class MerchantIssue(models.Model):
    """An issue a logged-in merchant raised from the merchant dashboard about one or more of their orders.

    Examples: an order the courier delivered but EscroSafe still shows differently, or a delivered order whose
    payment is delayed. The owner admin reads these in the admin app under "Merchant issues", changes the status
    and writes a response the merchant can read on their Support page.
    """

    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    # Keep in sync with ISSUE_TYPES in securepay_client/src/utils/issueTypes.js and securepay-admin.
    ISSUE_TYPE_CHOICES = [
        ("status_mismatch", "Delivered, but a different status is shown"),
        ("payment_delayed", "Delivered, but payment is delayed"),
        ("payment_missing", "Payment not received or wrong amount"),
        ("tracking_stuck", "Tracking is not updating"),
        ("rto_return", "Return or RTO not reflected"),
        ("customer_dispute", "Customer says the order was not delivered"),
        ("other", "Something else"),
    ]

    merchant_id = models.IntegerField(db_index=True)
    merchant_name = models.CharField(max_length=100, blank=True)
    merchant_email = models.EmailField(blank=True)
    issue_type = models.CharField(max_length=40, choices=ISSUE_TYPE_CHOICES, db_index=True)
    # The orders the merchant ticked: a list of {"order_id", "merchant_order_id", "awb", "courier",
    # "delivery_status", "payment_state"} captured when the issue was raised.
    orders = models.JSONField(default=list, blank=True)
    description = models.TextField()
    # Auto-generated when the ticket is raised: {merchant_order_id: report} with what the merchant saw on their
    # dashboard, what our system and the tracking source show, the customer's enquiries and the automatic findings.
    report = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    # Written by the admin; shown to the merchant on their Support page.
    admin_response = models.TextField(blank=True)
    # Written by the admin; never shown to the merchant.
    internal_note = models.TextField(blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_merchant_issue"
        ordering = ["-created_at", "-id"]

    @property
    def reference(self):
        return f"ISS-{self.id:05d}"

    def __str__(self):
        return f"{self.reference} {self.merchant_name} ({self.issue_type}, {self.status})"


class MerchantIssueMessage(models.Model):
    """One message in the conversation on a MerchantIssue (shown like a chat on both sides).

    The merchant's first message is the description they wrote when raising the issue. After that the merchant and
    the EscroSafe team (admin) write to each other from the merchant's Support page and the admin app.
    """

    SENDER_MERCHANT = "merchant"
    SENDER_ADMIN = "admin"
    SENDER_CHOICES = [
        (SENDER_MERCHANT, "Merchant"),
        (SENDER_ADMIN, "EscroSafe"),
    ]

    issue = models.ForeignKey(MerchantIssue, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=20, choices=SENDER_CHOICES, db_index=True)
    body = models.TextField()
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "admin_merchant_issue_message"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.issue_id} {self.sender} at {self.created_at}"


class MerchantIssueNote(models.Model):
    """An internal note an admin left on a MerchantIssue. Notes are only ever shown in the admin app, never to the merchant."""

    issue = models.ForeignKey(MerchantIssue, on_delete=models.CASCADE, related_name="notes")
    note = models.TextField()
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "admin_merchant_issue_note"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Note on issue {self.issue_id} at {self.created_at}"


class CustomerIssue(models.Model):
    """A ticket about one order raised on behalf of a customer (for example "I did not receive this order").

    Same shape as MerchantIssue: a reference (CIS-00001), a status, a chat between the customer and EscroSafe and
    internal notes only admins see. For now tickets are created from the admin app's "Customer issues" test form;
    the customer page can post here later.
    """

    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = MerchantIssue.STATUS_CHOICES

    # Keep in sync with ISSUE_TYPES in securepay-admin/src/pages/CustomerIssuesPage.jsx.
    ISSUE_TYPE_CHOICES = [
        ("not_received", "Order marked delivered but not received"),
        ("wrong_item", "Received a wrong item"),
        ("damaged", "Received a damaged item"),
        ("delayed", "Order is delayed"),
        ("refund", "Refund problem"),
        ("other", "Something else"),
    ]

    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=30, blank=True)
    customer_email = models.EmailField(blank=True)
    # What the customer typed: an order ID or an AWB number.
    order_ref = models.CharField(max_length=80, db_index=True)
    # Filled in when the order was found: {merchant_order_id, awb, courier, merchant_name, amount, delivery_status,
    # payment_state}. Empty when no order matches what the customer typed.
    order = models.JSONField(default=dict, blank=True)
    issue_type = models.CharField(max_length=40, choices=ISSUE_TYPE_CHOICES, db_index=True)
    description = models.TextField()
    # The extra questions the customer answered for this kind of problem: [{"key", "label", "value"}].
    details = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    # Where it came from: the public customer form records the visitor's IP (to limit spam); the admin test form leaves it empty.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_customer_issue"
        ordering = ["-created_at", "-id"]

    @property
    def reference(self):
        return f"CIS-{self.id:05d}"

    def __str__(self):
        return f"{self.reference} {self.customer_name} ({self.issue_type}, {self.status})"


class CustomerIssueMessage(models.Model):
    """One message in the chat on a CustomerIssue (the customer's words, or an EscroSafe reply)."""

    SENDER_CUSTOMER = "customer"
    SENDER_ADMIN = "admin"
    SENDER_CHOICES = [
        (SENDER_CUSTOMER, "Customer"),
        (SENDER_ADMIN, "EscroSafe"),
    ]

    issue = models.ForeignKey(CustomerIssue, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=20, choices=SENDER_CHOICES, db_index=True)
    body = models.TextField()
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "admin_customer_issue_message"
        ordering = ["created_at", "id"]


class CustomerIssueNote(models.Model):
    """An internal note an admin left on a CustomerIssue. Only ever shown in the admin app."""

    issue = models.ForeignKey(CustomerIssue, on_delete=models.CASCADE, related_name="notes")
    note = models.TextField()
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "admin_customer_issue_note"
        ordering = ["created_at", "id"]


def customer_issue_upload_path(instance, filename):
    """Random file names, so a photo's address cannot be guessed from the customer's own file name."""
    import os
    import uuid

    return f"customer_issues/{uuid.uuid4().hex}{os.path.splitext(filename)[1].lower()}"


class CustomerIssueAttachment(models.Model):
    """A photo the customer uploaded with a CustomerIssue (for example the damaged item)."""

    issue = models.ForeignKey(CustomerIssue, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=customer_issue_upload_path)
    original_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_customer_issue_attachment"
        ordering = ["created_at", "id"]
