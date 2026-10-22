-- Derived read-only views, dropped and recreated on every connect so edits here take effect.
-- Nothing is copied: they are queries over the source tables.

DROP VIEW IF EXISTS workouts;
-- Every reminder in any list whose name contains "workout" (latest version of each item,
-- deleted lists included), irregular notes too. Exercise items are "<name> - <reps> @ <n>lb";
-- their "Set N" subtasks carry the same parse via parent_title. Anything that doesn't fit
-- gets NULLs. `notes` is the raw text from the reminder's note field (mid-session "130lb",
-- "only 9 reps"); it is not parsed.
CREATE VIEW workouts AS
WITH latest AS (
    SELECT r.*, row_number() OVER (PARTITION BY external_id
                                   ORDER BY first_seen_at DESC, id DESC) AS rn
    FROM reminders r
    WHERE list_name LIKE '%workout%'),
src AS (
    SELECT *, coalesce(parent_title, title) AS item,
           parent_external_id IS NOT NULL AS is_subtask,
           stale_at IS NOT NULL AS deleted
    FROM latest WHERE rn = 1)
SELECT id AS reminder_id,
       list_name,
       substr(created_at, 1, 10) AS list_day,
       title,
       notes,
       parent_title,
       is_subtask,
       completed_at,
       deleted,
       CASE WHEN instr(item, ' - ') > 0 THEN trim(substr(item, 1, instr(item, ' - ') - 1)) END
           AS exercise,
       CASE WHEN instr(item, ' - ') > 0
            THEN nullif(cast(substr(item, instr(item, ' - ') + 3) AS INTEGER), 0) END
           AS target_reps,
       CASE WHEN instr(item, ' @ ') > 0
            THEN nullif(cast(substr(item, instr(item, ' @ ') + 3) AS REAL), 0) END
           AS target_weight_lb
FROM src;
