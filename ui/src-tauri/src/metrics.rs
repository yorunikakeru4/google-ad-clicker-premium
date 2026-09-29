//! Агрегаты дашборда фазы 4: чистое чтение сырых чисел из боевой БД.
//!
//! Пороги («не менее 50 запросов/час», «доля CAPTCHA < 5%») — решение UI:
//! здесь они не зашиты. Пустая база — нули, а не ошибка; нулевой знаменатель
//! доли капчи — `None`, а не ложные 0%.

use rusqlite::OptionalExtension;
use serde::Serialize;

use crate::db::{read_failed, DbError, DbReader};

/// Сводка по запускам сценария в окне `since`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RunsSummary {
    /// Статусы успеха (`ok`, `completed` — регистр не важен).
    pub succeeded: i64,
    /// Статусы ошибки (`failed`, `crashed`).
    pub failed: i64,
    /// Всё остальное: `running`, `stopped`, `baseline` и неизвестные статусы.
    pub other: i64,
    /// Текст самой свежей непустой `runs.error` в окне.
    pub last_error: Option<String>,
}

/// Часовой бакет кликов: `bucket` — unix-секунды начала часа.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HourlyClicks {
    pub bucket: i64,
    pub count: i64,
}

/// Скользящее окно «запросы/час» плюс разбивка по воркерам.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RequestsLastHour {
    pub total: i64,
    pub per_browser: Vec<BrowserRequests>,
}

/// Доля запросов одного `browser_id` в окне последнего часа.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BrowserRequests {
    /// `None`, если у запроса неизвестен браузер: такие строки тоже должны
    /// попадать в сумму, а не исчезать из нагрузки.
    pub browser_id: Option<String>,
    pub count: i64,
}

/// Воркер со свежим heartbeat — кандидат «жив» на дашборде.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ActiveWorker {
    pub browser_id: String,
    pub pid: Option<i64>,
    pub status: String,
    pub started_at: Option<f64>,
    pub heartbeat_at: f64,
    pub restart_count: i64,
    pub last_error: Option<String>,
}

/// Куда попадает статус запуска.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RunOutcome {
    Success,
    Failure,
    Other,
}

/// Классификация `runs.status` — единственное место, где известны словари
/// статусов. Регистр не важен: статусы пишут разные места кода, а дашборд
/// обязан их различать. Всё, что не в двух известных наборах, — «другое»:
/// так битые и устаревшие статусы не превращаются ни в успех, ни в ошибку.
fn classify_run_status(status: &str) -> RunOutcome {
    match status.trim().to_ascii_lowercase().as_str() {
        "ok" | "completed" => RunOutcome::Success,
        "failed" | "crashed" => RunOutcome::Failure,
        _ => RunOutcome::Other,
    }
}

impl DbReader {
    /// Сводка по `runs` в окне `since`: завершённые успешно / с ошибкой /
    /// другое плюс текст последней ошибки.
    ///
    /// Окно режется по `COALESCE(started_at, created_at)`: строка без
    /// `started_at` (старые и битые записи) не выпадает из дашборда. Класс —
    /// только по `status`: незавершённые (`running`), штатно остановленные
    /// (`stopped`) и неизвестные статусы идут в `other`. Последняя ошибка —
    /// самая свежая непустая `error` в окне независимо от статуса: текст
    /// полезен даже тогда, когда статус записан криво.
    pub fn runs_summary(&self, since: f64) -> Result<RunsSummary, DbError> {
        let mut summary = RunsSummary {
            succeeded: 0,
            failed: 0,
            other: 0,
            last_error: None,
        };

        let mut stmt = self
            .conn
            .prepare(
                "SELECT status, COUNT(*) AS n \
                 FROM runs \
                 WHERE COALESCE(started_at, created_at) >= ?1 \
                 GROUP BY status",
            )
            .map_err(read_failed)?;
        let rows = stmt
            .query_map(rusqlite::params![since], |row| {
                let status: String = row.get(0)?;
                let count: i64 = row.get(1)?;
                Ok((status, count))
            })
            .map_err(read_failed)?;
        for row in rows {
            let (status, count) = row.map_err(read_failed)?;
            match classify_run_status(&status) {
                RunOutcome::Success => summary.succeeded += count,
                RunOutcome::Failure => summary.failed += count,
                RunOutcome::Other => summary.other += count,
            }
        }

        summary.last_error = self
            .conn
            .query_row(
                "SELECT error FROM runs \
                 WHERE COALESCE(started_at, created_at) >= ?1 \
                   AND error IS NOT NULL AND TRIM(error) != '' \
                 ORDER BY id DESC LIMIT 1",
                rusqlite::params![since],
                |row| row.get(0),
            )
            .optional()
            .map_err(read_failed)?;

        Ok(summary)
    }

    /// Клики по часовым бакетам: только заполненные часы, по возрастанию.
    ///
    /// Час — полуинтервал `[t0, t0 + 3600)`: строка с `ts`, кратным часу,
    /// начинает новый час. Текущий неполный час включается — дашборд живой,
    /// нормализовать по доле часа решает UI; пропуски не нуляются. `buckets` —
    /// потолок самых свежих часов в окне `ts >= since`. `ts` — unix-секунды,
    /// поэтому деление на 3600 неотрицательное и `CAST` работает как floor.
    pub fn clicks_per_hour(&self, since: f64, buckets: u32) -> Result<Vec<HourlyClicks>, DbError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT CAST(ts / 3600 AS INTEGER) AS bucket, COUNT(*) AS n \
                 FROM clicks \
                 WHERE ts >= ?1 \
                 GROUP BY bucket \
                 ORDER BY bucket DESC \
                 LIMIT ?2",
            )
            .map_err(read_failed)?;
        let rows = stmt
            .query_map(rusqlite::params![since, buckets], |row| {
                let index: i64 = row.get(0)?;
                Ok(HourlyClicks {
                    bucket: index * 3600,
                    count: row.get(1)?,
                })
            })
            .map_err(read_failed)?;

        let mut points = rows
            .map(|row| row.map_err(read_failed))
            .collect::<Result<Vec<_>, _>>()?;
        // SQL отдаёт самые свежие часы первыми — дашборду нужен хронология.
        points.reverse();
        Ok(points)
    }

    /// Скользящее окно `[now - 3600, now]` по `network_requests`: сумма и
    /// разбивка по `browser_id` для нагрузки на воркер. Обе границы окна
    /// включаются.
    pub fn requests_last_hour(&self, now: f64) -> Result<RequestsLastHour, DbError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT browser_id, COUNT(*) AS n \
                 FROM network_requests \
                 WHERE ts >= ?1 AND ts <= ?2 \
                 GROUP BY browser_id",
            )
            .map_err(read_failed)?;
        let rows = stmt
            .query_map(rusqlite::params![now - 3600.0, now], |row| {
                Ok(BrowserRequests {
                    browser_id: row.get(0)?,
                    count: row.get(1)?,
                })
            })
            .map_err(read_failed)?;

        let mut per_browser = rows
            .map(|row| row.map_err(read_failed))
            .collect::<Result<Vec<_>, _>>()?;
        // Нагрузка вперёд: больше запросов — выше; ничья — browser_id по
        // возрастанию, NULL первый, как в ORDER BY SQLite.
        per_browser.sort_by(|left, right| {
            right
                .count
                .cmp(&left.count)
                .then_with(|| left.browser_id.cmp(&right.browser_id))
        });
        let total: i64 = per_browser.iter().map(|group| group.count).sum();

        Ok(RequestsLastHour { total, per_browser })
    }

    /// Доля капчи в окне `since`: `captcha_events / network_requests`.
    ///
    /// Знаменатель 0 даёт `None`, а не деление на ноль и не ложные 0% — UI
    /// покажет «н/д». Обе таблицы режутся одним `since`, поэтому доля не
    /// смешивает разные окна.
    pub fn captcha_share(&self, since: f64) -> Result<Option<f64>, DbError> {
        let (captchas, requests): (i64, i64) = self
            .conn
            .query_row(
                "SELECT \
                   (SELECT COUNT(*) FROM captcha_events WHERE ts >= ?1), \
                   (SELECT COUNT(*) FROM network_requests WHERE ts >= ?1)",
                rusqlite::params![since],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(read_failed)?;

        if requests == 0 {
            Ok(None)
        } else {
            Ok(Some(captchas as f64 / requests as f64))
        }
    }

    /// Воркеры с heartbeat не старше `threshold_secs` от `now` — живость для
    /// дашборда. Граница включается: heartbeat ровно на пороге считается
    /// живым, как и границы окна запросов; `heartbeat_at IS NULL` — не жив.
    /// Порог приходит из UI и здесь не зашит.
    pub fn active_workers(
        &self,
        now: f64,
        threshold_secs: u32,
    ) -> Result<Vec<ActiveWorker>, DbError> {
        let cutoff = now - f64::from(threshold_secs);
        let mut stmt = self
            .conn
            .prepare(
                "SELECT browser_id, pid, status, started_at, heartbeat_at, restart_count, last_error \
                 FROM workers \
                 WHERE heartbeat_at IS NOT NULL AND heartbeat_at >= ?1 \
                 ORDER BY browser_id",
            )
            .map_err(read_failed)?;
        let rows = stmt
            .query_map(rusqlite::params![cutoff], |row| {
                Ok(ActiveWorker {
                    browser_id: row.get(0)?,
                    pid: row.get(1)?,
                    status: row.get(2)?,
                    started_at: row.get(3)?,
                    heartbeat_at: row.get(4)?,
                    restart_count: row.get(5)?,
                    last_error: row.get(6)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::DbReader;
    use crate::test_support::{
        insert_captcha_event, insert_click, insert_network_request, insert_run, seed, RunRow,
        TempDb, WorkerRow,
    };

    // --- runs_summary ----------------------------------------------------

    #[test]
    fn runs_summary_on_empty_db_returns_zeros_not_error() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);

        let reader = DbReader::open(&path).expect("БД открывается");
        let summary = reader
            .runs_summary(0.0)
            .expect("пустая база — это нули, а не ошибка");

        assert_eq!(
            summary,
            RunsSummary {
                succeeded: 0,
                failed: 0,
                other: 0,
                last_error: None,
            }
        );
    }

    #[test]
    fn runs_summary_classifies_statuses_and_reports_last_error() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_run(&writer, &RunRow::new("ok").started(10.0).ended(20.0));
        insert_run(&writer, &RunRow::new("completed").started(11.0).ended(21.0));
        insert_run(&writer, &RunRow::new("OK").started(12.0).ended(22.0));
        insert_run(
            &writer,
            &RunRow::new("failed")
                .started(13.0)
                .error("selenium timeout"),
        );
        insert_run(
            &writer,
            &RunRow::new("crashed").started(14.0).error("exit code 1"),
        );
        insert_run(&writer, &RunRow::new("stopped").started(15.0).ended(25.0));
        insert_run(&writer, &RunRow::new("running").started(16.0));
        insert_run(&writer, &RunRow::new("baseline").started(17.0).ended(27.0));
        insert_run(&writer, &RunRow::new("weird_legacy_status").started(18.0));

        let reader = DbReader::open(&path).expect("БД открывается");
        let summary = reader.runs_summary(0.0).expect("сводка читается");

        assert_eq!(summary.succeeded, 3, "ok/completed, регистр не важен");
        assert_eq!(summary.failed, 2, "failed и crashed — неуспешные");
        assert_eq!(
            summary.other, 4,
            "running/stopped/baseline и битый статус — «другое»"
        );
        assert_eq!(
            summary.last_error.as_deref(),
            Some("exit code 1"),
            "последняя ошибка — самая свежая по порядку записи"
        );
    }

    #[test]
    fn runs_summary_window_is_inclusive_and_skips_older_runs() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_run(&writer, &RunRow::new("ok").started(4999.0).ended(4999.5));
        insert_run(
            &writer,
            &RunRow::new("failed").started(5000.0).error("граница окна"),
        );
        insert_run(
            &writer,
            &RunRow::new("crashed").started(6000.0).error("свежая"),
        );

        let reader = DbReader::open(&path).expect("БД открывается");
        let summary = reader.runs_summary(5000.0).expect("сводка читается");

        assert_eq!(
            summary.succeeded, 0,
            "запуск, начатый до since, не считается"
        );
        assert_eq!(summary.failed, 2, "граница окна включается");
        assert_eq!(summary.other, 0);
        assert_eq!(summary.last_error.as_deref(), Some("свежая"));
    }

    #[test]
    fn runs_summary_falls_back_to_created_at_when_started_at_is_null() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_run(&writer, &RunRow::new("ok").created(7000.0));

        let reader = DbReader::open(&path).expect("БД открывается");

        let inside = reader.runs_summary(7000.0).expect("окно по created_at");
        assert_eq!(
            inside.succeeded, 1,
            "битый запуск без started_at попадает в окно"
        );

        let outside = reader.runs_summary(7001.0).expect("окно уже закрыто");
        assert_eq!(
            outside,
            RunsSummary {
                succeeded: 0,
                failed: 0,
                other: 0,
                last_error: None,
            },
            "строка старше since не считается даже без started_at"
        );
    }

    // --- clicks_per_hour -------------------------------------------------

    #[test]
    fn clicks_per_hour_on_empty_db_returns_no_buckets() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);

        let reader = DbReader::open(&path).expect("БД открывается");
        let buckets = reader
            .clicks_per_hour(0.0, 24)
            .expect("пустая база — пустой график, а не ошибка");

        assert!(buckets.is_empty());
    }

    #[test]
    fn clicks_per_hour_groups_by_hour_with_inclusive_lower_boundary() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        for ts in [0.0, 59.9, 3600.0, 7199.5, 7200.0] {
            insert_click(&writer, ts);
        }

        let reader = DbReader::open(&path).expect("БД открывается");
        let buckets = reader.clicks_per_hour(0.0, 10).expect("бакеты читаются");

        assert_eq!(
            buckets,
            vec![
                HourlyClicks {
                    bucket: 0,
                    count: 2
                },
                HourlyClicks {
                    bucket: 3600,
                    count: 2
                },
                HourlyClicks {
                    bucket: 7200,
                    count: 1
                },
            ],
            "час [t0, t0+3600): граница начинает новый час, пропусков нет"
        );
    }

    #[test]
    fn clicks_per_hour_returns_latest_buckets_ascending() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        for bucket in [0_i64, 3600, 7200, 10800, 14400] {
            insert_click(&writer, bucket as f64 + 60.0);
        }

        let reader = DbReader::open(&path).expect("БД открывается");

        let latest = reader.clicks_per_hour(0.0, 2).expect("два свежих часа");
        assert_eq!(
            latest,
            vec![
                HourlyClicks {
                    bucket: 10800,
                    count: 1
                },
                HourlyClicks {
                    bucket: 14400,
                    count: 1
                },
            ],
            "buckets — это потолок свежих часов, порядок по возрастанию"
        );

        let all = reader.clicks_per_hour(0.0, 100).expect("все часы");
        assert_eq!(
            all.iter().map(|point| point.bucket).collect::<Vec<_>>(),
            vec![0, 3600, 7200, 10800, 14400],
            "широкий запас возвращает всю историю по возрастанию"
        );

        let none = reader
            .clicks_per_hour(0.0, 0)
            .expect("ноль бакетов — не ошибка");
        assert!(none.is_empty());
    }

    #[test]
    fn clicks_per_hour_since_filters_old_clicks_and_keeps_partial_current_hour() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        let now = 1_000_000.0;
        insert_click(&writer, now - 7300.0);
        insert_click(&writer, now - 3700.0);
        insert_click(&writer, now - 10.0);

        let reader = DbReader::open(&path).expect("БД открывается");
        let buckets = reader
            .clicks_per_hour(now - 7200.0, 24)
            .expect("бакеты читаются");

        let current_hour = ((now / 3600.0).floor() as i64) * 3600;
        assert_eq!(
            buckets,
            vec![
                HourlyClicks {
                    bucket: current_hour - 3600,
                    count: 1
                },
                HourlyClicks {
                    bucket: current_hour,
                    count: 1
                },
            ],
            "неполный текущий час показывается, клик старше since — нет"
        );
    }

    // --- requests_last_hour ----------------------------------------------

    #[test]
    fn requests_last_hour_on_empty_db_is_zero() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);

        let reader = DbReader::open(&path).expect("БД открывается");
        let load = reader
            .requests_last_hour(100.0)
            .expect("ноль запросов — не ошибка");

        assert_eq!(
            load,
            RequestsLastHour {
                total: 0,
                per_browser: Vec::new(),
            }
        );
    }

    #[test]
    fn requests_last_hour_counts_inclusive_window_boundaries() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        let now = 1_000_000.0;
        insert_network_request(&writer, now - 3600.0, None);
        insert_network_request(&writer, now, None);
        insert_network_request(&writer, now - 1800.0, None);
        insert_network_request(&writer, now - 3600.5, None);
        insert_network_request(&writer, now + 1.0, None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let load = reader.requests_last_hour(now).expect("окно читается");

        assert_eq!(
            load.total, 3,
            "окно [now-3600, now] включает обе границы и исключает внешние строки"
        );
    }

    #[test]
    fn requests_last_hour_breaks_down_by_browser() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        let now = 100.0;
        for ts in [1.0, 2.0, 3.0, 4.0] {
            insert_network_request(&writer, ts, Some("b1"));
        }
        for ts in [5.0, 6.0] {
            insert_network_request(&writer, ts, Some("b2"));
        }
        insert_network_request(&writer, 7.0, None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let load = reader.requests_last_hour(now).expect("разбивка читается");

        assert_eq!(load.total, 7);
        assert_eq!(
            load.per_browser,
            vec![
                BrowserRequests {
                    browser_id: Some("b1".to_string()),
                    count: 4
                },
                BrowserRequests {
                    browser_id: Some("b2".to_string()),
                    count: 2
                },
                BrowserRequests {
                    browser_id: None,
                    count: 1
                },
            ],
            "нагрузка по воркерам: по убыванию числа запросов"
        );
    }

    #[test]
    fn requests_last_hour_orders_ties_by_browser_id() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        let now = 100.0;
        for ts in [1.0, 2.0] {
            insert_network_request(&writer, ts, Some("b1"));
            insert_network_request(&writer, ts, Some("b2"));
            insert_network_request(&writer, ts, None);
        }

        let reader = DbReader::open(&path).expect("БД открывается");
        let load = reader.requests_last_hour(now).expect("разбивка читается");

        assert_eq!(
            load.per_browser,
            vec![
                BrowserRequests {
                    browser_id: None,
                    count: 2
                },
                BrowserRequests {
                    browser_id: Some("b1".to_string()),
                    count: 2
                },
                BrowserRequests {
                    browser_id: Some("b2".to_string()),
                    count: 2
                },
            ],
            "при равном счёте — browser_id по возрастанию, NULL первый"
        );
    }

    // --- captcha_share ----------------------------------------------------

    #[test]
    fn captcha_share_without_requests_is_none_not_division_by_zero() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_captcha_event(&writer, 1.0);
        insert_captcha_event(&writer, 2.0);
        insert_captcha_event(&writer, 3.0);

        let reader = DbReader::open(&path).expect("БД открывается");
        let share = reader
            .captcha_share(0.0)
            .expect("незнаменателя — не ошибка чтения");

        assert_eq!(share, None, "знаменатель 0 — None, UI покажет «н/д»");
    }

    #[test]
    fn captcha_share_is_zero_only_when_requests_exist_without_captchas() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        for ts in [1.0, 2.0, 3.0, 4.0, 5.0] {
            insert_network_request(&writer, ts, None);
        }

        let reader = DbReader::open(&path).expect("БД открывается");
        let share = reader.captcha_share(0.0).expect("доля читается");

        assert_eq!(
            share,
            Some(0.0),
            "запросы есть, капч нет — это настоящие 0%, а не «н/д»"
        );
    }

    #[test]
    fn captcha_share_counts_events_over_requests() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_captcha_event(&writer, 1.0);
        insert_captcha_event(&writer, 2.0);
        for ts in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0] {
            insert_network_request(&writer, ts, None);
        }

        let reader = DbReader::open(&path).expect("БД открывается");
        let share = reader.captcha_share(0.0).expect("доля читается");

        assert_eq!(share, Some(0.25), "2 капчи на 8 запросов");
    }

    #[test]
    fn captcha_share_window_covers_both_tables_inclusively() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        insert_captcha_event(&writer, 99.0);
        insert_captcha_event(&writer, 100.0);
        insert_network_request(&writer, 99.0, None);
        insert_network_request(&writer, 100.0, None);
        insert_network_request(&writer, 101.0, None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let share = reader.captcha_share(100.0).expect("доля читается");

        assert_eq!(
            share,
            Some(0.5),
            "обе таблицы режутся одним since: 1 капча на 2 запроса"
        );
    }

    // --- active_workers ---------------------------------------------------

    #[test]
    fn active_workers_on_empty_db_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);

        let reader = DbReader::open(&path).expect("БД открывается");
        let active = reader
            .active_workers(1000.0, 60)
            .expect("пустая база — пустой список, а не ошибка");

        assert!(active.is_empty());
    }

    #[test]
    fn active_workers_returns_fresh_heartbeats_including_threshold_boundary() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path);
        WorkerRow::new("w1")
            .heartbeat(999.0)
            .pid(4242)
            .status("running")
            .started(900.0)
            .last_error("прокси недоступен")
            .insert(&writer);
        WorkerRow::new("w2").heartbeat(940.0).insert(&writer);
        WorkerRow::new("w3").heartbeat(939.5).insert(&writer);
        WorkerRow::new("w4").insert(&writer);
        WorkerRow::new("w5").heartbeat(1005.0).insert(&writer);

        let reader = DbReader::open(&path).expect("БД открывается");
        let active = reader.active_workers(1000.0, 60).expect("живость читается");

        assert_eq!(
            active
                .iter()
                .map(|worker| worker.browser_id.as_str())
                .collect::<Vec<_>>(),
            vec!["w1", "w2", "w5"],
            "heartbeat ровно на пороге жив, старше порога и NULL — нет"
        );
        assert_eq!(
            active[0],
            ActiveWorker {
                browser_id: "w1".to_string(),
                pid: Some(4242),
                status: "running".to_string(),
                started_at: Some(900.0),
                heartbeat_at: 999.0,
                restart_count: 0,
                last_error: Some("прокси недоступен".to_string()),
            },
            "строка воркера отдаётся целиком для карточки в дашборде"
        );
    }
}
