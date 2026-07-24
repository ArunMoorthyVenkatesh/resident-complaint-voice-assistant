import os
import uuid
import logging
import boto3
from datetime import datetime, timezone, timedelta

SGT = timezone(timedelta(hours=8))
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger(__name__)

TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME", "BuildCareComplaints")
AWS_REGION  = os.getenv("AWS_REGION", "ap-southeast-1")

_table_cache = None


def _get_table(force_reconnect=False):
    global _table_cache
    if _table_cache is not None and not force_reconnect:
        return _table_cache

    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    _table_cache = dynamodb.Table(TABLE_NAME)
    return _table_cache


def _with_reconnect(fn):
    """Call fn(table), reconnecting once if the cached resource has expired."""
    global _table_cache
    try:
        return fn(_get_table())
    except Exception:
        _table_cache = None
        return fn(_get_table(force_reconnect=True))


def init_table():
    """Verify the DynamoDB table is accessible. Called on app startup."""
    try:
        table = _get_table()
        table.load()
        logger.info(f"DynamoDB table '{TABLE_NAME}' initialised successfully.")
    except Exception as e:
        logger.error(f"Failed to initialise DynamoDB table: {e}")


def save_complaint(complaint_data: dict) -> str:
    """Save a new complaint item and return the complaint_id (e.g. CMP-A1B2C3D4)."""
    complaint_id = "CMP-" + str(uuid.uuid4())[:8].upper()
    lang_map = {"en": "English", "ms": "Malay", "zh": "Chinese", "sg": "Singlish"}
    lang_code = complaint_data.get("language", "en")
    item = {
        "complaint_id":   complaint_id,
        "timestamp":      datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "name":           complaint_data.get("name", ""),
        "complaint_type": complaint_data.get("complaint_type", ""),
        "description":    complaint_data.get("description", ""),
        "location":       complaint_data.get("location", ""),
        "language":       lang_map.get(lang_code, lang_code),
        "status":         "Open",
    }
    _with_reconnect(lambda t: t.put_item(Item=item))
    logger.info(f"Complaint saved to DynamoDB: {complaint_id}")
    return complaint_id


VALID_STATUSES = ("Open", "In Progress", "Resolved", "Closed")


def update_status(complaint_id: str, status: str) -> None:
    """Update a complaint's status. Raises ValueError if status/complaint_id is invalid."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}.")

    def _update(t):
        t.update_item(
            Key={"complaint_id": complaint_id},
            UpdateExpression="SET #s = :status",
            ConditionExpression=Attr("complaint_id").exists(),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": status},
        )
    _with_reconnect(_update)
    logger.info(f"Complaint {complaint_id} status updated to '{status}'")


def get_all_complaints() -> list:
    """Return all complaints as a list of dicts."""
    def _scan(t):
        result = t.scan()
        items = result.get("Items", [])
        # Handle pagination
        while "LastEvaluatedKey" in result:
            result = t.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
            items.extend(result.get("Items", []))
        return items
    return _with_reconnect(_scan)



def clear_all_complaints():
    """Delete all complaint items from the table."""
    def _clear(t):
        result = t.scan(ProjectionExpression="complaint_id")
        items = result.get("Items", [])
        while "LastEvaluatedKey" in result:
            result = t.scan(
                ExclusiveStartKey=result["LastEvaluatedKey"],
                ProjectionExpression="complaint_id",
            )
            items.extend(result.get("Items", []))
        with t.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"complaint_id": item["complaint_id"]})
    _with_reconnect(_clear)
    logger.info("DynamoDB table cleared.")
