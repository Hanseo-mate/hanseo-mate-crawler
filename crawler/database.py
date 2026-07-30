import pymysql

from .config import DB_CONFIG
from .models import NoticeRecord


RETENTION_PERIOD_YEARS = 1


CREATE_NOTICES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    notice_type VARCHAR(50) NOT NULL,
    origin_notice_id VARCHAR(32) NOT NULL,
    title VARCHAR(500) NOT NULL,
    source_url VARCHAR(1024) NOT NULL,
    content_html LONGTEXT NOT NULL,
    author VARCHAR(100) NOT NULL,
    post_date DATE NOT NULL,
    is_hot BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uk_notices_notice_type_origin_notice_id (notice_type, origin_notice_id),
    KEY idx_notices_notice_type_post_date (notice_type, post_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# `content_html` is kept as LONGTEXT, while embedded Base64 images are extracted
# before insert so oversized inline payloads do not trigger `Data too long`.
ALTER_NOTICES_CONTENT_HTML_SQL = """
ALTER TABLE notices
MODIFY content_html LONGTEXT NOT NULL;
"""

ADD_NOTICES_SOURCE_URL_SQL = """
ALTER TABLE notices
ADD COLUMN source_url VARCHAR(1024) NOT NULL AFTER title;
"""

ADD_NOTICES_NOTICE_TYPE_SQL = """
ALTER TABLE notices
ADD COLUMN notice_type VARCHAR(50) NOT NULL DEFAULT 'academic' AFTER id;
"""

ADD_NOTICES_UNIQUE_KEY_SQL = """
ALTER TABLE notices
ADD UNIQUE KEY uk_notices_notice_type_origin_notice_id (notice_type, origin_notice_id);
"""

DROP_LEGACY_NOTICES_UNIQUE_KEY_SQL = """
ALTER TABLE notices
DROP INDEX uk_notices_origin_notice_id;
"""

ADD_NOTICES_TYPE_POST_DATE_INDEX_SQL = """
ALTER TABLE notices
ADD KEY idx_notices_notice_type_post_date (notice_type, post_date);
"""

CREATE_NOTICE_FILES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notice_files (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    notice_id BIGINT UNSIGNED NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_url VARCHAR(512) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_notice_files_notice_id (notice_id),
    CONSTRAINT fk_notice_files_notice_id
        FOREIGN KEY (notice_id) REFERENCES notices (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

INSERT_NOTICE_SQL = """
INSERT INTO notices (
    notice_type,
    origin_notice_id,
    title,
    source_url,
    content_html,
    author,
    post_date,
    is_hot
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
"""

UPDATE_NOTICE_SQL = """
UPDATE notices
SET title = %s,
    source_url = %s,
    content_html = %s,
    author = %s,
    post_date = %s,
    is_hot = %s
WHERE id = %s;
"""

SELECT_NOTICE_ID_SQL = """
SELECT id
FROM notices
WHERE notice_type = %s AND origin_notice_id = %s;
"""

SELECT_EXISTING_NOTICE_SUMMARIES_SQL_TEMPLATE = """
SELECT id, origin_notice_id, title, author, post_date, is_hot
FROM notices
WHERE notice_type = %s AND origin_notice_id IN ({placeholders});
"""

SELECT_NOTICE_DETAIL_SQL = """
SELECT id, title, source_url, content_html, author, post_date, is_hot
FROM notices
WHERE notice_type = %s AND origin_notice_id = %s;
"""

DELETE_NOTICE_FILES_SQL = """
DELETE FROM notice_files
WHERE notice_id = %s;
"""

INSERT_NOTICE_FILES_SQL = """
INSERT INTO notice_files (
    notice_id,
    file_name,
    file_url
) VALUES (%s, %s, %s);
"""

DELETE_EXPIRED_NOTICE_FILES_SQL = """
DELETE notice_files
FROM notice_files
INNER JOIN notices ON notices.id = notice_files.notice_id
WHERE notices.notice_type = %s
    AND notices.post_date < DATE_SUB(CURDATE(), INTERVAL %s YEAR);
"""

DELETE_EXPIRED_NOTICES_SQL = """
DELETE FROM notices
WHERE notice_type = %s
    AND post_date < DATE_SUB(CURDATE(), INTERVAL %s YEAR);
"""


def get_db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(**DB_CONFIG)


def _column_exists(cursor: pymysql.cursors.Cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        LIMIT 1;
        """,
        (DB_CONFIG["database"], table_name, column_name),
    )
    return cursor.fetchone() is not None


def _index_exists(cursor: pymysql.cursors.Cursor, table_name: str, index_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
        LIMIT 1;
        """,
        (DB_CONFIG["database"], table_name, index_name),
    )
    return cursor.fetchone() is not None


def ensure_schema(connection: pymysql.connections.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(CREATE_NOTICES_TABLE_SQL)

        if not _column_exists(cursor, "notices", "notice_type"):
            cursor.execute(ADD_NOTICES_NOTICE_TYPE_SQL)

        if not _column_exists(cursor, "notices", "source_url"):
            cursor.execute(ADD_NOTICES_SOURCE_URL_SQL)

        cursor.execute(ALTER_NOTICES_CONTENT_HTML_SQL)

        if _index_exists(cursor, "notices", "uk_notices_origin_notice_id"):
            cursor.execute(DROP_LEGACY_NOTICES_UNIQUE_KEY_SQL)

        if not _index_exists(cursor, "notices", "uk_notices_notice_type_origin_notice_id"):
            cursor.execute(ADD_NOTICES_UNIQUE_KEY_SQL)

        if not _index_exists(cursor, "notices", "idx_notices_notice_type_post_date"):
            cursor.execute(ADD_NOTICES_TYPE_POST_DATE_INDEX_SQL)

        cursor.execute(CREATE_NOTICE_FILES_TABLE_SQL)

    connection.commit()


def get_existing_notice_summaries(
    connection: pymysql.connections.Connection,
    notice_type: str,
    origin_notice_ids: list[str],
) -> dict[str, dict[str, object]]:
    if not origin_notice_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(origin_notice_ids))
    query = SELECT_EXISTING_NOTICE_SUMMARIES_SQL_TEMPLATE.format(placeholders=placeholders)

    with connection.cursor() as cursor:
        cursor.execute(query, (notice_type, *origin_notice_ids))
        rows = cursor.fetchall()

    return {
        row[1]: {
            "id": row[0],
            "title": row[2],
            "author": row[3],
            "post_date": row[4],
            "is_hot": bool(row[5]),
        }
        for row in rows
    }


def _attachments_equal(existing_attachments: list[tuple[str, str]], new_attachments: list[dict[str, str]]) -> bool:
    if len(existing_attachments) != len(new_attachments):
        return False

    normalized_new_attachments = [
        (attachment["file_name"], attachment["file_url"])
        for attachment in new_attachments
    ]
    return existing_attachments == normalized_new_attachments


def _fetch_notice_attachments(
    cursor: pymysql.cursors.Cursor,
    notice_id: int,
) -> list[tuple[str, str]]:
    cursor.execute(
        """
        SELECT file_name, file_url
        FROM notice_files
        WHERE notice_id = %s
        ORDER BY id ASC;
        """,
        (notice_id,),
    )
    return list(cursor.fetchall())


def insert_notice_if_absent(
    connection: pymysql.connections.Connection,
    notice: NoticeRecord,
) -> tuple[int, bool]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_NOTICE_ID_SQL, (notice.notice_type, notice.origin_notice_id))
            notice_row = cursor.fetchone()
            if notice_row is not None:
                connection.commit()
                return notice_row[0], False

            cursor.execute(
                INSERT_NOTICE_SQL,
                (
                    notice.notice_type,
                    notice.origin_notice_id,
                    notice.title,
                    notice.source_url,
                    notice.content_html,
                    notice.author,
                    notice.post_date,
                    notice.is_hot,
                ),
            )
            notice_id = cursor.lastrowid

            if notice.attachments:
                cursor.executemany(
                    INSERT_NOTICE_FILES_SQL,
                    [
                        (notice_id, attachment["file_name"], attachment["file_url"])
                        for attachment in notice.attachments
                    ],
                )

        connection.commit()
        return notice_id, True
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        if exc.args and exc.args[0] == 1062:
            with connection.cursor() as cursor:
                cursor.execute(SELECT_NOTICE_ID_SQL, (notice.notice_type, notice.origin_notice_id))
                notice_row = cursor.fetchone()
            if notice_row is None:
                raise ValueError(
                    f"공지 {notice.notice_type}/{notice.origin_notice_id}: 중복 감지 후 notice_id를 조회할 수 없습니다."
                ) from exc
            return notice_row[0], False
        raise
    except Exception:
        connection.rollback()
        raise


def sync_notice(
    connection: pymysql.connections.Connection,
    notice: NoticeRecord,
) -> tuple[int, str]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_NOTICE_DETAIL_SQL, (notice.notice_type, notice.origin_notice_id))
            existing_notice = cursor.fetchone()

            if existing_notice is None:
                cursor.execute(
                    INSERT_NOTICE_SQL,
                    (
                        notice.notice_type,
                        notice.origin_notice_id,
                        notice.title,
                        notice.source_url,
                        notice.content_html,
                        notice.author,
                        notice.post_date,
                        notice.is_hot,
                    ),
                )
                notice_id = cursor.lastrowid

                if notice.attachments:
                    cursor.executemany(
                        INSERT_NOTICE_FILES_SQL,
                        [
                            (notice_id, attachment["file_name"], attachment["file_url"])
                            for attachment in notice.attachments
                        ],
                    )

                connection.commit()
                return notice_id, "inserted"

            notice_id = existing_notice[0]
            existing_attachments = _fetch_notice_attachments(cursor, notice_id)
            notice_changed = (
                existing_notice[1] != notice.title
                or existing_notice[2] != notice.source_url
                or existing_notice[3] != notice.content_html
                or existing_notice[4] != notice.author
                or existing_notice[5] != notice.post_date
                or bool(existing_notice[6]) != notice.is_hot
            )
            attachments_changed = not _attachments_equal(existing_attachments, notice.attachments)

            if not notice_changed and not attachments_changed:
                connection.commit()
                return notice_id, "unchanged"

            if notice_changed:
                cursor.execute(
                    UPDATE_NOTICE_SQL,
                    (
                        notice.title,
                        notice.source_url,
                        notice.content_html,
                        notice.author,
                        notice.post_date,
                        notice.is_hot,
                        notice_id,
                    ),
                )

            if attachments_changed:
                cursor.execute(DELETE_NOTICE_FILES_SQL, (notice_id,))
                if notice.attachments:
                    cursor.executemany(
                        INSERT_NOTICE_FILES_SQL,
                        [
                            (notice_id, attachment["file_name"], attachment["file_url"])
                            for attachment in notice.attachments
                        ],
                    )

        connection.commit()
        return notice_id, "updated"
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        if exc.args and exc.args[0] == 1062:
            with connection.cursor() as cursor:
                cursor.execute(SELECT_NOTICE_ID_SQL, (notice.notice_type, notice.origin_notice_id))
                notice_row = cursor.fetchone()
            if notice_row is None:
                raise ValueError(
                    f"공지 {notice.notice_type}/{notice.origin_notice_id}: 중복 감지 후 notice_id를 조회할 수 없습니다."
                ) from exc
            return notice_row[0], "unchanged"
        raise
    except Exception:
        connection.rollback()
        raise


def delete_expired_notices(connection: pymysql.connections.Connection, notice_type: str) -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                DELETE_EXPIRED_NOTICE_FILES_SQL,
                (notice_type, RETENTION_PERIOD_YEARS),
            )
            affected_rows = cursor.execute(
                DELETE_EXPIRED_NOTICES_SQL,
                (notice_type, RETENTION_PERIOD_YEARS),
            )
        connection.commit()
        return affected_rows
    except Exception:
        connection.rollback()
        raise
