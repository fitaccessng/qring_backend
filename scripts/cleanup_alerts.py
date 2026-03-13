import sys

from sqlalchemy import text

from app.db.session import SessionLocal


def main() -> int:
    db = SessionLocal()
    try:
        total_deleted = 0

        # Remove orphaned related rows first.
        db.execute(
            text(
                "DELETE FROM homeowner_payments "
                "WHERE estate_alert_id NOT IN (SELECT id FROM estate_alerts)"
            )
        )
        db.execute(
            text(
                "DELETE FROM estate_meeting_responses "
                "WHERE estate_alert_id NOT IN (SELECT id FROM estate_alerts)"
            )
        )
        db.execute(
            text(
                "DELETE FROM estate_poll_votes "
                "WHERE estate_alert_id NOT IN (SELECT id FROM estate_alerts)"
            )
        )

        # Delete alerts with missing estates.
        result = db.execute(
            text(
                "DELETE FROM estate_alerts "
                "WHERE estate_id NOT IN (SELECT id FROM estates)"
            )
        )
        total_deleted += result.rowcount or 0

        # Delete alerts with empty titles.
        result = db.execute(
            text(
                "DELETE FROM estate_alerts "
                "WHERE title IS NULL OR TRIM(title) = ''"
            )
        )
        total_deleted += result.rowcount or 0

        # Normalize invalid alert_type to notice if possible, else delete.
        # If alert_type is a varchar, fix it. If enum, this will no-op.
        db.execute(
            text(
                "UPDATE estate_alerts "
                "SET alert_type = 'notice' "
                "WHERE alert_type IS NULL OR TRIM(alert_type) = ''"
            )
        )

        db.commit()
        print(f"Cleanup complete. Deleted alerts: {total_deleted}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Cleanup failed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
