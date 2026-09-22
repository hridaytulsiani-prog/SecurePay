# A registered merchant account. merchant_key/merchant_salt are used to sign
# hosted-checkout requests (see payments.auth.verify_merchant_auth_token);
# password is a sha512 hex digest, not a Django hasher (see merchant.v1.create_merchant).
from django.db import models

class MerchantInfo(models.Model):
    id = models.AutoField(primary_key=True)
    merchant_key = models.CharField(max_length=100, unique=True, null=False, blank=False)
    merchant_salt = models.CharField(max_length=100, unique=True, null=False, blank=False)
    merchant_name = models.CharField(max_length=100, null=False, blank=False)
    merchant_email = models.EmailField(null=False, blank=False)
    merchant_phone = models.CharField(max_length=15, null=False, blank=False)
    merchant_address = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    # username = models.CharField(max_length=100, null=False, blank=False)
    password = models.CharField(max_length=128, null=False, blank=False)
    courier_setup_completed = models.BooleanField(default=False)
    courier_preferences = models.JSONField(default=dict, blank=True)
    checkout_field_mapping = models.JSONField(default=dict, blank=True)
