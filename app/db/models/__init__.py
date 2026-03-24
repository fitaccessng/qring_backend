from __future__ import annotations

from importlib import import_module

_EXPORT_MAP = {
    "CommunityPost": "app.db.models.advanced",
    "CommunityPostRead": "app.db.models.advanced",
    "DigitalReceipt": "app.db.models.advanced",
    "EmergencySignal": "app.db.models.advanced",
    "PushSubscription": "app.db.models.advanced",
    "SplitBill": "app.db.models.advanced",
    "SplitContribution": "app.db.models.advanced",
    "ThreatAlertLog": "app.db.models.advanced",
    "VisitorRecognitionProfile": "app.db.models.advanced",
    "VisitorSnapshotAudit": "app.db.models.advanced",
    "WeeklySummaryLog": "app.db.models.advanced",
    "Appointment": "app.db.models.appointment",
    "DigitalAccessPass": "app.db.models.access_pass",
    "DeviceSession": "app.db.models.device_session",
    "EstateAlert": "app.db.models.estate_alert",
    "EstateAlertType": "app.db.models.estate_alert",
    "HomeownerPayment": "app.db.models.estate_alert",
    "HomeownerPaymentStatus": "app.db.models.estate_alert",
    "MaintenanceStatusAudit": "app.db.models.maintenance_audit",
    "EstateMeetingResponse": "app.db.models.estate_engagement",
    "EstatePollVote": "app.db.models.estate_engagement",
    "MeetingResponseType": "app.db.models.estate_engagement",
    "Door": "app.db.models.estate",
    "Estate": "app.db.models.estate",
    "Home": "app.db.models.estate",
    "HomeownerSetting": "app.db.models.homeowner_setting",
    "AuditLog": "app.db.models.audit",
    "GateLog": "app.db.models.audit",
    "HomeownerWallet": "app.db.models.payment",
    "HomeownerWalletTransaction": "app.db.models.payment",
    "PaymentPurpose": "app.db.models.payment",
    "Subscription": "app.db.models.payment",
    "SubscriptionEvent": "app.db.models.subscription_policy",
    "SubscriptionInvoice": "app.db.models.subscription_policy",
    "SubscriptionNotification": "app.db.models.subscription_policy",
    "SubscriptionPlan": "app.db.models.payment",
    "PaymentAttempt": "app.db.models.subscription_policy",
    "ReferralReward": "app.db.models.referral_reward",
    "QRCode": "app.db.models.qr_code",
    "CallSession": "app.db.models.session",
    "EmergencyAlert": "app.db.models.safety",
    "EmergencyAlertEvent": "app.db.models.safety",
    "EmergencyAlertPriority": "app.db.models.safety",
    "EmergencyAlertStatus": "app.db.models.safety",
    "EmergencyAlertType": "app.db.models.safety",
    "AlertDeliveryStatus": "app.db.models.safety",
    "Message": "app.db.models.session",
    "Notification": "app.db.models.session",
    "VisitorReport": "app.db.models.safety",
    "VisitorSession": "app.db.models.session",
    "VisitorReportSeverity": "app.db.models.safety",
    "VisitorReportStatus": "app.db.models.safety",
    "WatchlistEntry": "app.db.models.safety",
    "WatchlistRiskLevel": "app.db.models.safety",
    "User": "app.db.models.user",
    "UserRole": "app.db.models.user",
}

__all__ = list(_EXPORT_MAP.keys())


def __getattr__(name: str):
    module_name = _EXPORT_MAP.get(name)
    if not module_name:
        raise AttributeError(name)
    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
