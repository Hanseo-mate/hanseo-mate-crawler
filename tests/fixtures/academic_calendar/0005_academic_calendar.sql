-- MySQL 8. Existing manual events are preserved. Re-runnable after partial DDL failure.

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='source')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT ''MANUAL''', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='source_event_id')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN source_event_id VARCHAR(100) NULL', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='academic_year')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN academic_year INT NULL', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='semester')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN semester INT NULL', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='event_type')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN event_type VARCHAR(30) NOT NULL DEFAULT ''OTHER''', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='source_url')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN source_url VARCHAR(1000) NULL', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='last_synced_at')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN last_synced_at DATETIME(6) NULL', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='school_calendar_events' AND column_name='active')=0,
 'ALTER TABLE school_calendar_events ADD COLUMN active BIT(1) NOT NULL DEFAULT b''1''', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

SET @academic_ddl = IF(
 (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE()
  AND table_name='school_calendar_events' AND index_name='uk_school_calendar_source')=0,
 'CREATE UNIQUE INDEX uk_school_calendar_source ON school_calendar_events(source, academic_year, semester, source_event_id)', 'SELECT 1');
PREPARE academic_stmt FROM @academic_ddl;
EXECUTE academic_stmt;
DEALLOCATE PREPARE academic_stmt;

CREATE TABLE IF NOT EXISTS academic_holiday_months (
 month_key VARCHAR(7) NOT NULL PRIMARY KEY,
 holiday_dates VARCHAR(400) NOT NULL,
 synced_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

