import pymysql

from .config import DB_CONFIG
from .models import NoticeRecord


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
# before upsert so oversized inline payloads do not trigger `Data too long`.
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

UPSERT_NOTICE_SQL = """
INSERT INTO notices (
    notice_type,
    origin_notice_id,
    title,
    source_url,
    content_html,
    author,
    post_date,
    is_hot
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    title = VALUES(title),
    source_url = VALUES(source_url),
    content_html = VALUES(content_html),
    author = VALUES(author),
    post_date = VALUES(post_date),
    is_hot = VALUES(is_hot);
"""

SELECT_NOTICE_ID_SQL = """
SELECT id
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

DELETE_EXPIRED_NOTICES_SQL = """
DELETE FROM notices
WHERE post_date <= DATE_SUB(CURDATE(), INTERVAL 2 YEAR);
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


def sync_notice(
    connection: pymysql.connections.Connection,
    notice: NoticeRecord,
) -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                UPSERT_NOTICE_SQL,
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
            cursor.execute(SELECT_NOTICE_ID_SQL, (notice.notice_type, notice.origin_notice_id))
            notice_row = cursor.fetchone()
            if notice_row is None:
                raise ValueError(
                    f"공지 {notice.notice_type}/{notice.origin_notice_id}: upsert 후 notice_id를 조회할 수 없습니다."
                )

            notice_id = notice_row[0]
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
        return notice_id
    except Exception:
        connection.rollback()
        raise


def delete_expired_notices(connection: pymysql.connections.Connection) -> int:
    with connection.cursor() as cursor:
        affected_rows = cursor.execute(DELETE_EXPIRED_NOTICES_SQL)
    connection.commit()
    return affected_rows
