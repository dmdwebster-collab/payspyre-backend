from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.notification import (
    Delivery, Notification, NotificationPreference, NotificationTemplate, WebhookDelivery
)
from app.schemas.notification import (
    BulkNotificationCreate, BulkNotificationResponse, DeliveryResponse,
    NotificationCreate, NotificationPreferenceCreate, NotificationPreferenceResponse,
    NotificationPreferenceUpdate, NotificationQueueResponse,
    NotificationResponse, NotificationTemplateCreate, NotificationTemplateResponse,
    NotificationTemplateUpdate, NotificationUpdate, WebhookDeliveryCreate, WebhookDeliveryResponse
)
from app.services.notifications import NotificationQueue

router = APIRouter()


def get_notification_queue(db: Session = Depends(get_db)) -> NotificationQueue:
    return NotificationQueue(db)


@router.post("/templates", response_model=NotificationTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_template(
    template_data: NotificationTemplateCreate,
    db: Session = Depends(get_db),
) -> NotificationTemplate:
    existing = db.query(NotificationTemplate).filter(
        NotificationTemplate.name == template_data.name
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template with this name already exists",
        )

    template = NotificationTemplate(**template_data.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.get("/templates", response_model=list[NotificationTemplateResponse])
async def list_templates(
    type: str | None = Query(None, pattern="^(email|sms)$"),
    category: str | None = Query(None, pattern="^(application_status|payment_reminder|statement|urgent|marketing|system)$"),
    is_active: bool | None = None,
    db: Session = Depends(get_db),
) -> list[NotificationTemplate]:
    query = db.query(NotificationTemplate)

    if type:
        query = query.filter(NotificationTemplate.type == type)

    if category:
        query = query.filter(NotificationTemplate.category == category)

    if is_active is not None:
        query = query.filter(NotificationTemplate.is_active == is_active)

    return query.all()


@router.get("/templates/{template_id}", response_model=NotificationTemplateResponse)
async def get_template(
    template_id: UUID,
    db: Session = Depends(get_db),
) -> NotificationTemplate:
    template = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id
    ).first()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )

    return template


@router.put("/templates/{template_id}", response_model=NotificationTemplateResponse)
async def update_template(
    template_id: UUID,
    template_data: NotificationTemplateUpdate,
    db: Session = Depends(get_db),
) -> NotificationTemplate:
    template = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id
    ).first()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )

    update_data = template_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(template, field, value)

    db.commit()
    db.refresh(template)
    return template


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    template = db.query(NotificationTemplate).filter(
        NotificationTemplate.id == template_id
    ).first()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )

    db.delete(template)
    db.commit()


@router.post("/queue", response_model=NotificationResponse, status_code=status.HTTP_201_CREATED)
async def queue_notification(
    notification_data: NotificationCreate,
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
) -> Notification:
    notification = await queue.queue_notification(notification_data.model_dump())
    background_tasks.add_task(queue.process_queue)
    return notification


@router.post("/queue/bulk", response_model=BulkNotificationResponse, status_code=status.HTTP_201_CREATED)
async def queue_bulk_notifications(
    bulk_data: BulkNotificationCreate,
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
) -> BulkNotificationResponse:
    notifications = await queue.queue_bulk_notifications(
        recipients=bulk_data.recipients,
        notification_data={
            "type": bulk_data.type,
            "template_id": bulk_data.template_id,
            "subject": bulk_data.subject,
            "body": bulk_data.body,
            "variables": bulk_data.variables,
            "scheduled_for": bulk_data.scheduled_for,
            "priority": bulk_data.priority,
        },
    )

    background_tasks.add_task(queue.process_queue)

    return BulkNotificationResponse(
        notification_ids=[n.id for n in notifications],
        total=len(notifications),
        success=len(notifications),
        failed=0,
    )


@router.get("/queue/stats", response_model=NotificationQueueResponse)
async def get_queue_stats(
    queue: NotificationQueue = Depends(get_notification_queue),
) -> NotificationQueueResponse:
    stats = queue.get_queue_stats()
    return NotificationQueueResponse(
        queued_count=stats.get("queued", 0),
        processing_count=stats.get("processing", 0),
        failed_count=stats.get("failed", 0),
        retry_count=stats.get("retrying", 0),
    )


@router.post("/queue/process", response_model=dict[str, int])
async def process_queue(
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
) -> dict[str, int]:
    background_tasks.add_task(queue.process_queue)
    return {"message": "Queue processing started"}


@router.post("/queue/retry", response_model=dict[str, int])
async def retry_failed_notifications(
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
) -> dict[str, int]:
    background_tasks.add_task(queue.retry_failed_notifications)
    return {"message": "Retry processing started"}


@router.get("/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    user_id: UUID | None = Query(None),
    loan_application_id: UUID | None = Query(None),
    vendor_id: UUID | None = Query(None),
    type: str | None = Query(None, pattern="^(email|sms|webhook)$"),
    status: str | None = Query(None, pattern="^(queued|processing|sent|delivered|failed|retrying)$"),
    priority: str | None = Query(None, pattern="^(low|normal|high|urgent)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Notification]:
    query = db.query(Notification)

    if user_id:
        query = query.filter(Notification.user_id == user_id)

    if loan_application_id:
        query = query.filter(Notification.loan_application_id == loan_application_id)

    if vendor_id:
        query = query.filter(Notification.vendor_id == vendor_id)

    if type:
        query = query.filter(Notification.type == type)

    if status:
        query = query.filter(Notification.status == status)

    if priority:
        query = query.filter(Notification.priority == priority)

    return query.order_by(Notification.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/notifications/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
) -> Notification:
    notification = db.query(Notification).filter(
        Notification.id == notification_id
    ).first()

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    return notification


@router.put("/notifications/{notification_id}", response_model=NotificationResponse)
async def update_notification(
    notification_id: UUID,
    notification_data: NotificationUpdate,
    db: Session = Depends(get_db),
) -> Notification:
    notification = db.query(Notification).filter(
        Notification.id == notification_id
    ).first()

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    update_data = notification_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(notification, field, value)

    db.commit()
    db.refresh(notification)
    return notification


@router.delete("/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    notification = db.query(Notification).filter(
        Notification.id == notification_id
    ).first()

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    db.delete(notification)
    db.commit()


@router.get("/notifications/{notification_id}/deliveries", response_model=list[DeliveryResponse])
async def get_notification_deliveries(
    notification_id: UUID,
    db: Session = Depends(get_db),
) -> list[Delivery]:
    deliveries = db.query(Delivery).filter(
        Delivery.notification_id == notification_id
    ).order_by(Delivery.created_at.desc()).all()

    return deliveries


@router.post("/preferences", response_model=NotificationPreferenceResponse, status_code=status.HTTP_201_CREATED)
async def create_preferences(
    prefs_data: NotificationPreferenceCreate,
    db: Session = Depends(get_db),
) -> NotificationPreference:
    existing = db.query(NotificationPreference).filter(
        NotificationPreference.user_id == prefs_data.user_id,
        NotificationPreference.vendor_id == prefs_data.vendor_id,
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preferences already exist for this user/vendor combination",
        )

    preferences = NotificationPreference(**prefs_data.model_dump())
    db.add(preferences)
    db.commit()
    db.refresh(preferences)
    return preferences


@router.get("/preferences/{user_id}", response_model=NotificationPreferenceResponse)
async def get_preferences(
    user_id: UUID,
    vendor_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> NotificationPreference:
    query = db.query(NotificationPreference).filter(
        NotificationPreference.user_id == user_id,
    )

    if vendor_id:
        query = query.filter(NotificationPreference.vendor_id == vendor_id)
    else:
        query = query.filter(NotificationPreference.vendor_id.is_(None))

    preferences = query.first()

    if not preferences:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found",
        )

    return preferences


@router.put("/preferences/{user_id}", response_model=NotificationPreferenceResponse)
async def update_preferences(
    user_id: UUID,
    prefs_data: NotificationPreferenceUpdate,
    vendor_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> NotificationPreference:
    query = db.query(NotificationPreference).filter(
        NotificationPreference.user_id == user_id,
    )

    if vendor_id:
        query = query.filter(NotificationPreference.vendor_id == vendor_id)
    else:
        query = query.filter(NotificationPreference.vendor_id.is_(None))

    preferences = query.first()

    if not preferences:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found",
        )

    update_data = prefs_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(preferences, field, value)

    db.commit()
    db.refresh(preferences)
    return preferences


@router.post("/webhooks", response_model=WebhookDeliveryResponse, status_code=status.HTTP_201_CREATED)
async def send_webhook(
    webhook_data: WebhookDeliveryCreate,
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
    db: Session = Depends(get_db),
) -> WebhookDelivery:
    async def send_and_update():
        success, delivery_id = await queue.send_webhook(
            vendor_id=webhook_data.vendor_id,
            event_type=webhook_data.event_type,
            url=webhook_data.url,
            payload=webhook_data.payload,
        )
        return {"success": success, "delivery_id": str(delivery_id)}

    background_tasks.add_task(send_and_update)

    delivery = WebhookDelivery(
        vendor_id=webhook_data.vendor_id,
        event_type=webhook_data.event_type,
        url=webhook_data.url,
        payload=webhook_data.payload,
        status="pending",
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


@router.get("/webhooks/{webhook_id}", response_model=WebhookDeliveryResponse)
async def get_webhook_delivery(
    webhook_id: UUID,
    db: Session = Depends(get_db),
) -> WebhookDelivery:
    delivery = db.query(WebhookDelivery).filter(
        WebhookDelivery.id == webhook_id
    ).first()

    if not delivery:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook delivery not found",
        )

    return delivery


@router.get("/webhooks", response_model=list[WebhookDeliveryResponse])
async def list_webhook_deliveries(
    vendor_id: UUID | None = Query(None),
    event_type: str | None = Query(None),
    status: str | None = Query(None, pattern="^(pending|sent|failed)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[WebhookDelivery]:
    query = db.query(WebhookDelivery)

    if vendor_id:
        query = query.filter(WebhookDelivery.vendor_id == vendor_id)

    if event_type:
        query = query.filter(WebhookDelivery.event_type == event_type)

    if status:
        query = query.filter(WebhookDelivery.status == status)

    return query.order_by(WebhookDelivery.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/webhooks/retry", response_model=dict[str, int])
async def retry_pending_webhooks(
    background_tasks: BackgroundTasks,
    queue: NotificationQueue = Depends(get_notification_queue),
) -> dict[str, int]:
    background_tasks.add_task(queue.retry_pending_webhooks)
    return {"message": "Webhook retry processing started"}


@router.get("/users/{user_id}/check-preferences")
async def check_notification_preferences(
    user_id: UUID,
    notification_type: str = Query(..., pattern="^(email|sms)$"),
    category: str = Query(..., pattern="^(application_status|payment_reminder|statement|urgent|marketing|system)$"),
    queue: NotificationQueue = Depends(get_notification_queue),
) -> dict[str, Any]:
    allowed = queue.check_user_preferences(
        user_id=user_id,
        notification_type=notification_type,
        category=category,
    )

    quiet_hours = queue.is_quiet_hours(user_id=user_id)

    return {
        "allowed": allowed,
        "quiet_hours": quiet_hours,
        "user_id": str(user_id),
        "notification_type": notification_type,
        "category": category,
    }