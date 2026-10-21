-- SQLite 3 with JSON1. UTC timestamps are ISO 8601 text; domain.schema.json owns JSON shapes.
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT INTO app_meta VALUES('schema_version','4.0'),('content_revision','0');
CREATE TABLE institutions(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL CHECK(type IN ('childcare','kindergarten','elementary','middle','high','special_school','other_school','university','junior_college')),
 district TEXT NOT NULL CHECK(district IN ('동구','서구','남구','북구','광산구')),address TEXT NOT NULL,campus_label TEXT,
 phone TEXT,homepage_url TEXT,operation_status TEXT NOT NULL CHECK(operation_status IN ('active','temporarily_closed','operation_review','closed_confirmed','unknown')),
 operation_effective_date TEXT,operation_evidence_url TEXT,source_checked_at TEXT,
 official_channels_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(official_channels_json)),
 profile_overrides_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(profile_overrides_json)),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE institution_identifiers(
 provider TEXT NOT NULL,external_key TEXT NOT NULL,institution_id TEXT NOT NULL REFERENCES institutions(id),
 PRIMARY KEY(provider,external_key)
);
CREATE TABLE registry_snapshots(
 id TEXT PRIMARY KEY,source_json TEXT NOT NULL CHECK(json_valid(source_json)),retrieved_at TEXT NOT NULL,
 loaded_count INTEGER NOT NULL CHECK(loaded_count>=0),linked_count INTEGER NOT NULL CHECK(linked_count>=0),
 rejected_count INTEGER NOT NULL CHECK(rejected_count>=0),source_file_ref TEXT
);
CREATE TABLE registry_memberships(
 snapshot_id TEXT NOT NULL REFERENCES registry_snapshots(id),institution_id TEXT NOT NULL REFERENCES institutions(id),
 external_key TEXT NOT NULL,raw_row_json TEXT NOT NULL CHECK(json_valid(raw_row_json)),
 PRIMARY KEY(snapshot_id,external_key)
);
CREATE TABLE seasons(
 id TEXT PRIMARY KEY,label TEXT NOT NULL,academic_year INTEGER CHECK(academic_year BETWEEN 2000 AND 2100),
 search_start TEXT NOT NULL,search_end TEXT NOT NULL CHECK(search_end>=search_start),export_key TEXT UNIQUE NOT NULL,
 timezone TEXT NOT NULL CHECK(timezone='Asia/Seoul'),region_profile TEXT NOT NULL CHECK(region_profile='gwangju_5_gu'),revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE events(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),season_id TEXT NOT NULL REFERENCES seasons(id),
 event_key TEXT NOT NULL,title TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('graduation_ceremony','graduation_and_completion','completion_ceremony','degree_award_ceremony','unknown')),
 audience TEXT,campus_label TEXT,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 last_attempt_at TEXT,last_successful_source_check_at TEXT,
 change_check_status TEXT NOT NULL DEFAULT 'not_checked' CHECK(change_check_status IN ('not_checked','completed','limited')),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(institution_id,season_id,event_key)
);
CREATE TABLE documents(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),source_key TEXT NOT NULL,
 display_url TEXT NOT NULL,title TEXT NOT NULL,authority TEXT NOT NULL CHECK(authority IN ('official','unverified','external')),
 acquisition_json TEXT NOT NULL DEFAULT '{"method":"unknown","transport_status":"not_run","http_status":null,"final_url":null,"tls_verified":false}' CHECK(json_valid(acquisition_json)),reading_json TEXT NOT NULL DEFAULT '{"method":"none","scope":"none","content_match":"unchecked","institution_match":"unchecked"}' CHECK(json_valid(reading_json)),
 image_size_json TEXT CHECK(image_size_json IS NULL OR json_valid(image_size_json)),
 source_kind TEXT NOT NULL CHECK(source_kind IN ('official_api','html','pdf','image','attachment_other','unknown','hwp','hwpx')),content_type TEXT,
 status TEXT NOT NULL CHECK(status IN ('ok','access_failed','unsupported_format','parse_failed','too_large','content_mismatch')),
 retrieved_at TEXT NOT NULL,last_checked_at TEXT,content_hash TEXT,
 segments_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(segments_json)),partial INTEGER NOT NULL DEFAULT 0 CHECK(partial IN (0,1)),
 read_scope TEXT NOT NULL DEFAULT '',reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_codes_json)),
 authority_evidence_json TEXT NOT NULL DEFAULT 'null' CHECK(json_valid(authority_evidence_json)),
 recheck_json TEXT NOT NULL DEFAULT '{"status":"not_attempted","attempted_at":null,"reason_codes":[]}' CHECK(json_valid(recheck_json)),
 published_at TEXT,modified_at TEXT,etag TEXT,cache_ref TEXT,UNIQUE(institution_id,source_key,content_hash)
);
CREATE TABLE observations(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),season_id TEXT NOT NULL REFERENCES seasons(id),
 event_id TEXT REFERENCES events(id),document_id TEXT NOT NULL REFERENCES documents(id),
 candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),fingerprint TEXT NOT NULL UNIQUE,
 disposition TEXT NOT NULL DEFAULT 'pending' CHECK(disposition IN ('pending','accepted','reference','rejected')),reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_codes_json)),created_at TEXT NOT NULL
);
CREATE TABLE assessments(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,event_id TEXT NOT NULL REFERENCES events(id),
 field TEXT NOT NULL CHECK(field IN ('ceremony_status','event_date','start_time','venue')),status TEXT NOT NULL CHECK(status IN ('unchecked','official_confirmed','reference_only','missing','conflict')),
 automatic_json TEXT NOT NULL CHECK(json_valid(automatic_json)),created_at TEXT NOT NULL
);
CREATE INDEX assessments_latest ON assessments(event_id,field,seq DESC);
CREATE TABLE manual_overrides(
 id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events(id),field TEXT NOT NULL CHECK(field IN ('ceremony_status','event_date','start_time','venue')),
 mode TEXT NOT NULL CHECK(mode IN ('set','clear')),value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 basis TEXT NOT NULL CHECK(basis IN ('phone_confirmed','document_confirmed','user_recorded')),bound_date TEXT,confirmed_at TEXT,recorded_at TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
 acknowledged_fingerprint TEXT,resolved_fingerprint TEXT,source_url TEXT,source_document_id TEXT REFERENCES documents(id),active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE UNIQUE INDEX one_active_override ON manual_overrides(event_id,field) WHERE active=1;
CREATE TABLE audit_log(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,field TEXT,
 action TEXT NOT NULL,actor TEXT NOT NULL CHECK(actor IN ('user','system')),recorded_at TEXT NOT NULL,
 before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)),summary TEXT NOT NULL
);
CREATE TABLE jobs(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('schedule_scan','registry_sync')),season_id TEXT REFERENCES seasons(id),
 status TEXT NOT NULL CHECK(status IN ('queued','running','pausing','paused','cancelling','cancelled','completed','failed')),paused_reason TEXT,
 request_json TEXT NOT NULL CHECK(json_valid(request_json)),scope_snapshot_json TEXT NOT NULL CHECK(json_valid(scope_snapshot_json)),limits_json TEXT NOT NULL CHECK(json_valid(limits_json)),
 usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),target_count INTEGER NOT NULL CHECK(target_count>=0),
 idempotency_key TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
-- scope_snapshot_json의 source_config_hash는 비밀 제외 실행 설정을 고정한다.
-- 재개 전 현재 hash와 대조하며 불일치는 API 409로 거절한다. 키는 저장하지 않는다.
CREATE TABLE job_items(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),target_ref TEXT NOT NULL,institution_id TEXT REFERENCES institutions(id),
 status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed','cancelled')),stage TEXT NOT NULL,outcome TEXT,
 reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_codes_json)),attempt INTEGER NOT NULL DEFAULT 0,
 usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),history_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(history_json)),
 last_attempt_at TEXT,last_successful_source_check_at TEXT,error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
 UNIQUE(job_id,target_ref)
);
CREATE INDEX job_items_pending ON job_items(job_id,status);
CREATE TABLE job_segments(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),segment_number INTEGER NOT NULL,
 started_at TEXT NOT NULL,ended_at TEXT,last_heartbeat_at TEXT NOT NULL,
 usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),UNIQUE(job_id,segment_number)
);
CREATE TABLE call_reservations(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),job_item_id TEXT NOT NULL REFERENCES job_items(id),segment_id TEXT NOT NULL REFERENCES job_segments(id),
 call_kind TEXT NOT NULL CHECK(call_kind IN ('http','llm')),status TEXT NOT NULL CHECK(status IN ('reserved','settled','charged_unknown')),
 reserved_json TEXT NOT NULL CHECK(json_valid(reserved_json)),actual_json TEXT CHECK(actual_json IS NULL OR json_valid(actual_json)),
 started_at TEXT NOT NULL,deadline_at TEXT NOT NULL,finished_at TEXT
);
CREATE TABLE import_previews(
 id TEXT PRIMARY KEY,preview_hash TEXT NOT NULL,content_revision INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),preview_json TEXT NOT NULL CHECK(json_valid(preview_json)),
 status TEXT NOT NULL CHECK(status IN ('preview','committed','expired')),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 idempotency_key TEXT UNIQUE,result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json))
);
CREATE TABLE exports(
 season_id TEXT PRIMARY KEY REFERENCES seasons(id),content_revision INTEGER NOT NULL,exported_revision INTEGER,
 exported_at TEXT,status TEXT NOT NULL CHECK(status IN ('current','pending','failed')),include_notes INTEGER NOT NULL DEFAULT 0 CHECK(include_notes IN (0,1)),
 publication_policy_version TEXT,file_ref TEXT,error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json))
);

CREATE TABLE request_keys(
 key TEXT PRIMARY KEY,route TEXT NOT NULL,payload_hash TEXT NOT NULL,
 response_status INTEGER NOT NULL,response_json TEXT NOT NULL CHECK(json_valid(response_json)),created_at TEXT NOT NULL
);
