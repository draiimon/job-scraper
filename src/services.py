from __future__ import annotations
import asyncio, hashlib, logging, random, json, re, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from .config import Settings
from .jobs import NormalizedJob, canonicalize_url, clean, enrich_canonical, evaluate, extract_skills, is_ph_location, freshness, is_active_listing
from .models import Base, Job, JobStatus, SourceRun, SourceHealth, AppState, AppSetting, ResumeProfile, JobEvent, JobCompany, JobSource
from .security import ActionTokens
log=logging.getLogger(__name__)
DISCORD_ALERT_ROLE_ALLOWLIST={'1345727357662658603'}
# A notification confirms delivery; it is never an application-status decision.
# Only a newly discovered job may become NOTIFIED.  Every other state (saved,
# ignored, reviewing, applied, rejected, and later application outcomes) wins
# if delivery races with a user action.
NOTIFICATION_MUTABLE_STATUSES=frozenset({
    JobStatus.NEW.value,
    JobStatus.NOTIFIED.value,
})
class Repository:
    def __init__(self, url: str):
        # Supabase commonly supplies postgresql:// URLs; explicitly select the
        # bundled psycopg v3 dialect instead of SQLAlchemy's psycopg2 default.
        if url.startswith('postgresql://'):
            url='postgresql+psycopg://'+url.removeprefix('postgresql://')
        elif url.startswith('postgres://'):
            url='postgresql+psycopg://'+url.removeprefix('postgres://')
        if url.startswith('sqlite:///') and not url.startswith('sqlite:////'):
            Path(url.removeprefix('sqlite:///')).parent.mkdir(parents=True, exist_ok=True)
        connect_args={'check_same_thread':False} if url.startswith('sqlite') else {}
        engine_options={}
        if url.startswith('postgresql+'):
            # Keep one process well below hosted PostgreSQL session caps. Scan
            # work can fan out, but callers should queue for a small pool
            # instead of creating a connection per source thread.
            engine_options.update(
                pool_size=3,
                max_overflow=1,
                pool_timeout=60,
                pool_recycle=900,
                pool_pre_ping=True,
                pool_use_lifo=True,
            )
        self.engine=create_engine(url,connect_args=connect_args,**engine_options); self.sessions=sessionmaker(self.engine,expire_on_commit=False)
    def create_schema(self):
        Base.metadata.create_all(self.engine)
        # create_all does not alter an existing column.  This is a safe,
        # widening-only migration for databases created before scan counters
        # were persisted in the scheduler snapshot.
        if self.engine.dialect.name == 'postgresql':
            app_state_columns = {column['name']: column for column in inspect(self.engine).get_columns('app_state')}
            job_columns = {column['name'] for column in inspect(self.engine).get_columns('jobs')}
            health_columns = {column['name'] for column in inspect(self.engine).get_columns('source_health')}
            run_columns = {column['name'] for column in inspect(self.engine).get_columns('source_runs')}
            existing_by_table = {
                'jobs': job_columns,
                'source_runs': run_columns,
                'source_health': health_columns,
            }
            with self.engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '10s'"))
                connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                if app_state_columns and str(app_state_columns.get('value',{}).get('type','')).upper() != 'TEXT':
                    connection.execute(text('ALTER TABLE app_state ALTER COLUMN value TYPE TEXT'))
                if 'last_seen_at' not in job_columns:
                    connection.execute(text('ALTER TABLE jobs ADD COLUMN last_seen_at TIMESTAMP WITH TIME ZONE'))
                    connection.execute(text('UPDATE jobs SET last_seen_at = date_discovered WHERE last_seen_at IS NULL'))
                if 'identity_key' not in job_columns:
                    connection.execute(text('ALTER TABLE jobs ADD COLUMN identity_key VARCHAR(64)'))
                    connection.execute(text("UPDATE jobs SET identity_key = fingerprint WHERE identity_key IS NULL OR identity_key = ''"))
                connection.execute(text('CREATE INDEX IF NOT EXISTS ix_jobs_identity_key ON jobs (identity_key)'))
                if 'last_raw_jobs' not in health_columns: connection.execute(text('ALTER TABLE source_health ADD COLUMN last_raw_jobs INTEGER DEFAULT 0'))
                if 'last_normalized_jobs' not in health_columns: connection.execute(text('ALTER TABLE source_health ADD COLUMN last_normalized_jobs INTEGER DEFAULT 0'))
                if 'last_accepted_jobs' not in health_columns: connection.execute(text('ALTER TABLE source_health ADD COLUMN last_accepted_jobs INTEGER DEFAULT 0'))
                for table, columns in {
                    'jobs': {
                        'company_domain': 'VARCHAR(255)', 'country': 'VARCHAR(80)', 'requirements': "TEXT NOT NULL DEFAULT ''",
                        'salary_min': 'DOUBLE PRECISION', 'salary_max': 'DOUBLE PRECISION', 'salary_currency': 'VARCHAR(8)',
                        'posted_at': 'TIMESTAMP WITH TIME ZONE', 'first_seen_at': 'TIMESTAMP WITH TIME ZONE',
                        'updated_at': 'TIMESTAMP WITH TIME ZONE', 'expires_at': 'TIMESTAMP WITH TIME ZONE',
                        'experience_min': 'DOUBLE PRECISION', 'experience_max': 'DOUBLE PRECISION', 'category': 'VARCHAR(80)',
                        'notification_attempts': 'INTEGER NOT NULL DEFAULT 0', 'last_notification_attempt': 'TIMESTAMP WITH TIME ZONE',
                        'notification_next_attempt_at': 'TIMESTAMP WITH TIME ZONE', 'notification_error': 'TEXT',
                        'content_hash': 'VARCHAR(64)', 'source_hash': 'VARCHAR(64)', 'is_active': 'BOOLEAN NOT NULL DEFAULT TRUE',
                        'is_duplicate': 'BOOLEAN NOT NULL DEFAULT FALSE', 'duplicate_of': 'INTEGER',
                    },
                    'source_runs': {
                        'jobs_updated': 'INTEGER NOT NULL DEFAULT 0', 'duplicates': 'INTEGER NOT NULL DEFAULT 0',
                        'duration_seconds': 'DOUBLE PRECISION', 'status': "VARCHAR(24) NOT NULL DEFAULT 'RUNNING'",
                    },
                    'source_health': {
                        'health_score': 'DOUBLE PRECISION NOT NULL DEFAULT 1', 'last_duration_seconds': 'DOUBLE PRECISION',
                        'last_new_jobs': 'INTEGER NOT NULL DEFAULT 0', 'last_duplicates': 'INTEGER NOT NULL DEFAULT 0',
                    },
                }.items():
                    # Never inspect through a second pooled connection while this
                    # transaction owns an ALTER TABLE lock. PostgreSQL makes the
                    # inspector wait for this transaction, producing a self-
                    # deadlock during startup.
                    existing = existing_by_table[table]
                    for name, ddl in columns.items():
                        if name not in existing:
                            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}'))
                connection.execute(text('UPDATE jobs SET posted_at = date_posted WHERE posted_at IS NULL'))
                connection.execute(text('UPDATE jobs SET first_seen_at = date_discovered WHERE first_seen_at IS NULL'))
                connection.execute(text('UPDATE jobs SET is_active = FALSE WHERE status = \'EXPIRED\''))
        elif self.engine.dialect.name == 'sqlite':
            columns = inspect(self.engine).get_columns('app_state')
            value_column = next((column for column in columns if column['name'] == 'value'), None)
            if value_column is not None and getattr(value_column['type'], 'length', None) == 500:
                # SQLite has no ALTER COLUMN. Rebuild only this small key/value
                # table and copy every existing row before dropping the legacy
                # table, so local deployments receive the same widening as
                # PostgreSQL without losing scheduler state.
                with self.engine.begin() as connection:
                    connection.exec_driver_sql('ALTER TABLE app_state RENAME TO app_state_legacy')
                    connection.exec_driver_sql(
                        'CREATE TABLE app_state ('
                        '"key" VARCHAR(100) NOT NULL, '
                        'value TEXT NOT NULL, '
                        'PRIMARY KEY ("key")'
                        ')'
                    )
                    connection.exec_driver_sql(
                        'INSERT INTO app_state ("key", value) '
                        'SELECT "key", value FROM app_state_legacy'
                    )
                    connection.exec_driver_sql('DROP TABLE app_state_legacy')
            job_columns = {column['name'] for column in inspect(self.engine).get_columns('jobs')}
            if 'last_seen_at' not in job_columns:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql('ALTER TABLE jobs ADD COLUMN last_seen_at DATETIME')
                    connection.exec_driver_sql('UPDATE jobs SET last_seen_at = date_discovered WHERE last_seen_at IS NULL')
            if 'identity_key' not in job_columns:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN identity_key VARCHAR(64) DEFAULT ''")
                    connection.exec_driver_sql("UPDATE jobs SET identity_key = fingerprint WHERE identity_key IS NULL OR identity_key = ''")
                    connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_jobs_identity_key ON jobs (identity_key)')
            health_columns = {column['name'] for column in inspect(self.engine).get_columns('source_health')}
            for name in ('last_raw_jobs', 'last_normalized_jobs', 'last_accepted_jobs'):
                if name not in health_columns:
                    with self.engine.begin() as connection:
                        connection.exec_driver_sql(f'ALTER TABLE source_health ADD COLUMN {name} INTEGER DEFAULT 0')
            sqlite_columns = {
                'jobs': {
                    'company_domain': 'VARCHAR(255)', 'country': 'VARCHAR(80)', 'requirements': "TEXT NOT NULL DEFAULT ''",
                    'salary_min': 'FLOAT', 'salary_max': 'FLOAT', 'salary_currency': 'VARCHAR(8)', 'posted_at': 'DATETIME',
                    'first_seen_at': 'DATETIME', 'updated_at': 'DATETIME', 'expires_at': 'DATETIME', 'experience_min': 'FLOAT',
                    'experience_max': 'FLOAT', 'category': 'VARCHAR(80)', 'notification_attempts': 'INTEGER NOT NULL DEFAULT 0',
                    'last_notification_attempt': 'DATETIME', 'notification_next_attempt_at': 'DATETIME', 'notification_error': 'TEXT',
                    'content_hash': 'VARCHAR(64)', 'source_hash': 'VARCHAR(64)', 'is_active': 'BOOLEAN NOT NULL DEFAULT 1',
                    'is_duplicate': 'BOOLEAN NOT NULL DEFAULT 0', 'duplicate_of': 'INTEGER',
                },
                'source_runs': {'jobs_updated': 'INTEGER NOT NULL DEFAULT 0', 'duplicates': 'INTEGER NOT NULL DEFAULT 0', 'duration_seconds': 'FLOAT', 'status': "VARCHAR(24) NOT NULL DEFAULT 'RUNNING'"},
                'source_health': {'health_score': 'FLOAT NOT NULL DEFAULT 1', 'last_duration_seconds': 'FLOAT', 'last_new_jobs': 'INTEGER NOT NULL DEFAULT 0', 'last_duplicates': 'INTEGER NOT NULL DEFAULT 0'},
            }
            for table, columns in sqlite_columns.items():
                existing = {column['name'] for column in inspect(self.engine).get_columns(table)}
                for name, ddl in columns.items():
                    if name not in existing:
                        with self.engine.begin() as connection:
                            connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
            with self.engine.begin() as connection:
                connection.exec_driver_sql('UPDATE jobs SET posted_at = date_posted WHERE posted_at IS NULL')
                connection.exec_driver_sql('UPDATE jobs SET first_seen_at = date_discovered WHERE first_seen_at IS NULL')
                connection.exec_driver_sql("UPDATE jobs SET is_active = 0 WHERE status = 'EXPIRED'")
        self._create_runtime_indexes()

    def _create_runtime_indexes(self):
        """Add only the indexes used by discovery, dedupe, and notifications."""
        statements = (
            'CREATE INDEX IF NOT EXISTS ix_jobs_posted_active_score ON jobs (posted_at, is_active, score)',
            'CREATE INDEX IF NOT EXISTS ix_jobs_notification_due ON jobs (notification_state, notification_next_attempt_at)',
            'CREATE INDEX IF NOT EXISTS ix_jobs_source_job_id ON jobs (source, source_job_id)',
            'CREATE INDEX IF NOT EXISTS ix_source_health_status ON source_health (status, last_checked_at)',
            'CREATE INDEX IF NOT EXISTS ix_source_runs_completed ON source_runs (completed_at, success)',
        )
        with self.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    def initialize_runtime_config(self, cfg: Settings) -> dict[str, str]:
        """Create/load safe runtime settings and hydrate the process config."""
        self.create_schema()
        defaults = {
            'jobstreet_enabled': 'true',
            'jobstreet_base_url': 'https://ph.jobstreet.com',
            'jobstreet_login_url': '',
            'jobstreet_location': 'Philippines',
            'jobstreet_search_terms_json': '["software", "developer", "IT support", "technical support", "DevOps", "cloud", "infrastructure", "systems", "network", "QA", "cybersecurity", "application support"]',
            'jobstreet_max_results': '50',
            'jobstreet_max_pages': '5',
            'jobstreet_auth_timeout_seconds': '600',
            'jobstreet_scan_timeout_seconds': '60',
            'jobstreet_last_verified': '',
            'jobstreet_status': 'AUTH REQUIRED',
        }
        with self.sessions.begin() as session:
            for key, value in defaults.items():
                if session.get(AppSetting, key) is None:
                    session.add(AppSetting(key=key, value=value))
        with self.sessions() as session:
            values = {
                row.key: row.value
                for row in session.scalars(select(AppSetting)).all()
            }
        enabled = str(values.get('jobstreet_enabled', 'true')).strip().lower()
        cfg.jobstreet_enabled = enabled not in {'0', 'false', 'no', 'off'}
        for key in (
            'jobstreet_base_url',
            'jobstreet_login_url',
            'jobstreet_location',
            'jobstreet_search_terms_json',
        ):
            value = str(values.get(key) or '')
            if value:
                setattr(cfg, key, value)
        for key in (
            'jobstreet_max_results',
            'jobstreet_max_pages',
            'jobstreet_auth_timeout_seconds',
            'jobstreet_scan_timeout_seconds',
        ):
            try:
                setattr(cfg, key, int(values[key]))
            except (KeyError, TypeError, ValueError):
                pass
        return values

    def set_setting(self, key: str, value: str) -> None:
        with self.sessions.begin() as session:
            setting = session.get(AppSetting, key)
            if setting is None:
                session.add(AppSetting(key=key, value=str(value)))
            else:
                setting.value = str(value)

    def upsert_source_target(self, target: dict, discovery_method: str = 'configured') -> JobSource:
        """Persist a validated public ATS target and its company provenance."""
        provider = str(target.get('kind') or target.get('provider') or '').lower()
        identifier = str(target.get('token') or target.get('site') or target.get('board') or target.get('company') or '').strip()
        name = str(target.get('name') or identifier).strip()
        if not provider or not identifier or not name:
            raise ValueError('source target requires provider, identifier, and name')
        base_url = str(target.get('base_url') or target.get('career_url') or '').strip()
        with self.sessions.begin() as session:
            company = session.scalar(select(JobCompany).where(JobCompany.name == name))
            if company is None:
                company = JobCompany(name=name, domain=target.get('domain'), country=target.get('country') or 'Philippines', career_url=target.get('career_url'), ats_provider=provider, ats_identifier=identifier, discovery_method=discovery_method)
                session.add(company)
                session.flush()
            else:
                company.domain = target.get('domain') or company.domain
                company.career_url = target.get('career_url') or company.career_url
                company.ats_provider = provider
                company.ats_identifier = identifier
                company.last_discovered = datetime.now(timezone.utc)
                company.discovery_method = discovery_method or company.discovery_method
                company.active = True
            source = session.scalar(select(JobSource).where(JobSource.provider == provider, JobSource.board_identifier == identifier))
            if source is None:
                source = JobSource(company_id=company.id, provider=provider, board_identifier=identifier, base_url=base_url or f'{provider}://{identifier}', discovery_method=discovery_method)
                session.add(source)
            else:
                source.company_id = company.id
                source.base_url = base_url or source.base_url
                source.enabled = True
                source.discovery_method = discovery_method or source.discovery_method
                source.health_score = max(source.health_score or 0, 0.25)
            return source

    def registry_targets(self) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(select(JobSource).where(JobSource.enabled.is_(True)).order_by(JobSource.id)).all()
            companies = {company.id: company for company in session.scalars(select(JobCompany)).all()}
        targets = []
        for row in rows:
            company = companies.get(row.company_id)
            target = {'kind': row.provider, 'name': company.name if company else row.board_identifier, 'token': row.board_identifier}
            if row.provider == 'lever': target['site'] = row.board_identifier
            if row.provider == 'ashby': target['board'] = row.board_identifier
            if row.provider == 'smartrecruiters': target['company'] = row.board_identifier
            if company and company.domain: target['domain'] = company.domain
            if company and company.career_url: target['career_url'] = company.career_url
            targets.append(target)
        return targets

    def source_registry_snapshot(self) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(select(JobSource).order_by(JobSource.provider, JobSource.board_identifier)).all()
            companies = {company.id: company for company in session.scalars(select(JobCompany)).all()}
        return [{
            'id': row.id, 'provider': row.provider, 'board_identifier': row.board_identifier,
            'company': companies.get(row.company_id).name if companies.get(row.company_id) else None,
            'enabled': row.enabled, 'health_score': row.health_score, 'last_success': row.last_success,
            'last_attempt': row.last_attempt, 'last_error': row.last_error, 'discovery_method': row.discovery_method,
        } for row in rows]

    def record_source_registry_result(self, source_name: str, success: bool, error: str | None = None) -> None:
        """Mirror runtime health onto discovered-source registry rows."""
        if ':' not in source_name:
            return
        provider, company_name = source_name.split(':', 1)
        if provider not in {'greenhouse', 'lever', 'ashby', 'smartrecruiters'}:
            return
        now=datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            rows=session.scalars(
                select(JobSource)
                .join(JobCompany, JobCompany.id == JobSource.company_id)
                .where(JobSource.provider == provider, JobCompany.name == company_name)
            ).all()
            for row in rows:
                row.last_attempt=now
                if success:
                    row.last_success=now
                    row.last_error=None
                    row.health_score=min(1.0,(row.health_score or 0.0)+0.1)
                else:
                    row.last_error=(error or 'source fetch failed')[:1000]
                    row.health_score=max(0.0,(row.health_score if row.health_score is not None else 1.0)-0.15)

    @staticmethod
    def _provenance(item: NormalizedJob) -> dict:
        metadata=item.raw_metadata or {}
        return {
            'source': item.source,
            'source_job_id': item.source_job_id,
            'discovery_url': metadata.get('discovery_url') or item.url,
            'canonical_url': metadata.get('canonical_url') or item.application_url or item.url,
        }

    @staticmethod
    def _stored_canonical_url(job: Job) -> str:
        metadata=job.raw_metadata or {}
        return canonicalize_url(metadata.get('canonical_url') or metadata.get('direct_apply_url') or job.application_url or job.url)

    @staticmethod
    def _incoming_canonical_url(item: NormalizedJob) -> str:
        metadata=item.raw_metadata or {}
        return canonicalize_url(metadata.get('canonical_url') or metadata.get('direct_apply_url') or item.application_url or item.url)

    @staticmethod
    def _descriptions_match(left: str, right: str) -> bool:
        left_clean=clean(left or '')
        right_clean=clean(right or '')
        if not left_clean or not right_clean:
            return False
        if left_clean == right_clean:
            return True
        left_terms=set(re.findall(r'[a-z0-9]{3,}', left_clean))
        right_terms=set(re.findall(r'[a-z0-9]{3,}', right_clean))
        if len(left_terms) < 6 or len(right_terms) < 6:
            return False
        return len(left_terms & right_terms) / min(len(left_terms), len(right_terms)) >= 0.72

    def _same_vacancy(self, existing: Job, item: NormalizedJob) -> bool:
        """Require evidence beyond company/title/location before merging."""
        existing_url=self._stored_canonical_url(existing)
        incoming_url=self._incoming_canonical_url(item)
        if existing_url and incoming_url and existing_url == incoming_url:
            return True
        if existing.source == item.source and existing.source_job_id and item.source_job_id:
            if existing.source_job_id == item.source_job_id:
                return True
        return self._descriptions_match(existing.description, item.description)

    @staticmethod
    def _identity_key(item: NormalizedJob) -> str:
        return item.fingerprint

    def _merge_duplicate_provenance(self, existing: Job, item: NormalizedJob) -> None:
        """Keep cross-source evidence without creating duplicate canonical jobs."""
        metadata=dict(existing.raw_metadata or {})
        provenance=list(metadata.get('source_provenance') or [])
        incoming=self._provenance(item)
        key=(incoming.get('source'),incoming.get('source_job_id'),incoming.get('canonical_url'))
        if not any((row.get('source'),row.get('source_job_id'),row.get('canonical_url'))==key for row in provenance if isinstance(row,dict)):
            provenance.append(incoming)
        metadata['source_provenance']=provenance[-8:]

        incoming_direct=(item.raw_metadata or {}).get('direct_apply_url')
        incoming_canonical=incoming.get('canonical_url')
        existing_direct=metadata.get('direct_apply_url')
        official_ats=item.source.startswith(('greenhouse:','lever:','ashby:'))
        if incoming_canonical and ((incoming_direct and not existing_direct) or (existing.source.startswith('jobspy:') and official_ats)):
            existing.url=incoming_canonical
            existing.application_url=incoming_canonical
            metadata['canonical_url']=incoming_canonical
            if incoming_direct:
                metadata['direct_apply_url']=incoming_direct
        existing.raw_metadata=metadata

    def save(self, item: NormalizedJob, score:int, reasons:list[str], warnings:list[str]) -> Job | None:
        return self.save_with_result(item,score,reasons,warnings)[0]

    def save_with_result(self, item: NormalizedJob, score:int, reasons:list[str], warnings:list[str]) -> tuple[Job | None,str]:
        with self.sessions() as s:
            seen_at=datetime.now(timezone.utc)
            source_hash=hashlib.sha256(f'{item.source}|{item.source_job_id or item.url}'.encode('utf-8')).hexdigest()
            content_hash=item.content_hash
            updated_at=(item.raw_metadata or {}).get('updated_at') or item.date_posted
            if isinstance(updated_at,str):
                try: updated_at=datetime.fromisoformat(updated_at.replace('Z','+00:00'))
                except ValueError: updated_at=item.date_posted
            fields={
                'company_domain':item.company_domain, 'country':item.country, 'requirements':item.requirements or item.description,
                'salary_min':item.salary_min, 'salary_max':item.salary_max, 'salary_currency':item.salary_currency,
                'updated_at':updated_at,
                'expires_at':item.expires_at, 'experience_min':item.experience_min, 'experience_max':item.experience_max,
                'category':item.category, 'content_hash':content_hash, 'source_hash':source_hash, 'is_active':item.is_active,
            }
            identity_key=self._identity_key(item)
            candidates=s.scalars(select(Job).where(Job.identity_key==identity_key)).all()
            # Rows created before the migration used ``fingerprint`` alone.
            # The fallback makes mixed-version deployments deduplicate safely
            # before their schema upgrade completes.
            if not candidates:
                candidates=s.scalars(select(Job).where(Job.fingerprint==identity_key)).all()
            existing=next((candidate for candidate in candidates if self._same_vacancy(candidate,item)), None)
            # A changed source ID plus a genuinely newer source timestamp is a
            # repost/reactivation, not a duplicate discovery of an old listing.
            if existing:
                stored_date=existing.date_posted.replace(tzinfo=timezone.utc) if existing.date_posted and existing.date_posted.tzinfo is None else existing.date_posted
                repost=(existing.source==item.source and item.source_job_id and item.source_job_id!=existing.source_job_id and item.date_posted and (not stored_date or item.date_posted>stored_date))
                if not repost:
                    material_change=bool(existing.content_hash and existing.content_hash!=content_hash) or (not existing.content_hash and clean(existing.description)!=clean(item.description))
                    reactivated=existing.status==JobStatus.EXPIRED.value and item.is_active
                    existing.last_seen_at=seen_at
                    existing.title=item.title; existing.company=item.company; existing.location=item.location
                    existing.work_setup=item.work_setup or item.remote_type; existing.description=item.description
                    existing.url=item.url; existing.application_url=item.application_url
                    existing.application_email=item.application_email; existing.salary=item.salary
                    existing.employment_type=item.employment_type; existing.seniority=item.seniority
                    existing.skills=extract_skills(item); existing.score=score
                    existing.match_reasons=reasons; existing.warnings=warnings
                    if not existing.date_posted and item.date_posted:
                        existing.date_posted=item.date_posted; existing.posted_at=item.date_posted
                    for key,value in fields.items():
                        if value is not None or key in {'is_active','requirements','content_hash','source_hash'}:
                            setattr(existing,key,value)
                    self._merge_duplicate_provenance(existing,item)
                    if reactivated:
                        existing.status=JobStatus.NEW.value; existing.notification_state='PENDING'
                        existing.notification_error=None; existing.notification_next_attempt_at=None
                    s.commit()
                    return (existing,'reposted') if reactivated else (None,'updated' if material_change else 'duplicate')
                existing.source_job_id=item.source_job_id; existing.date_posted=item.date_posted; existing.posted_at=item.date_posted; existing.last_seen_at=seen_at; existing.url=item.url; existing.application_url=item.application_url; existing.description=item.description; existing.score=score; existing.match_reasons=reasons; existing.warnings=warnings; existing.raw_metadata={**item.raw_metadata,'reposted':True,'source_provenance':[self._provenance(item)]}; existing.status=JobStatus.NEW.value; existing.notification_state='PENDING'; existing.notification_error=None; existing.notification_next_attempt_at=None
                for key,value in fields.items():
                    if value is not None or key in {'is_active','requirements','content_hash','source_hash'}:
                        setattr(existing,key,value)
                s.commit(); return existing,'reposted'
            # A same-company/title/location collision without matching source
            # evidence is a genuinely distinct vacancy. Keep its broad bucket
            # in ``identity_key`` but use the stable source-aware token for
            # the unique storage fingerprint.
            stored_fingerprint=item.fingerprint if not candidates else item.dedup_token
            job=Job(fingerprint=stored_fingerprint,identity_key=identity_key,source=item.source,source_job_id=item.source_job_id,title=item.title,company=item.company,company_domain=item.company_domain,location=item.location,country=item.country,work_setup=item.work_setup or item.remote_type,description=item.description,requirements=item.requirements or item.description,url=item.url,application_url=item.application_url,application_email=item.application_email,salary=item.salary,salary_min=item.salary_min,salary_max=item.salary_max,salary_currency=item.salary_currency,date_posted=item.date_posted,posted_at=item.date_posted,date_discovered=seen_at,first_seen_at=seen_at,last_seen_at=seen_at,updated_at=fields['updated_at'],expires_at=item.expires_at,employment_type=item.employment_type,seniority=item.seniority,experience_min=item.experience_min,experience_max=item.experience_max,category=item.category,skills=extract_skills(item),raw_metadata={**(item.raw_metadata or {}),'source_provenance':[self._provenance(item)]},score=score,match_reasons=reasons,warnings=warnings,content_hash=content_hash,source_hash=source_hash,is_active=item.is_active)
            s.add(job)
            try: s.commit(); return job,'new'
            except IntegrityError: s.rollback(); return None,'duplicate'
    def run_start(self, source):
        with self.sessions() as s: x=SourceRun(source=source); s.add(x); s.commit(); return x.id
    def run_finish(self,id,**values):
        with self.sessions() as s:
            x=s.get(SourceRun,id)
            for k,v in values.items(): setattr(x,k,v)
            x.completed_at=datetime.now(timezone.utc); s.commit()
    def health(self, source):
        with self.sessions() as s:
            item=s.get(SourceHealth,source)
            if not item: item=SourceHealth(source=source); s.add(item); s.commit()
            return item
    def health_success(self, source, jobs, baseline=False, status='healthy', error=None, raw_jobs=None, normalized_jobs=None, accepted_jobs=None, duration_seconds=None, new_jobs=0, duplicates=0):
        with self.sessions() as s:
            item=s.get(SourceHealth,source) or SourceHealth(source=source); s.add(item)
            item.last_checked_at=item.last_success_at=datetime.now(timezone.utc); item.last_job_count=jobs
            item.last_raw_jobs=jobs if raw_jobs is None else raw_jobs
            item.last_normalized_jobs=jobs if normalized_jobs is None else normalized_jobs
            item.last_accepted_jobs=jobs if accepted_jobs is None else accepted_jobs
            item.consecutive_failures=0; item.last_error=error; item.status=status; item.health_score=min(1.0,(item.health_score or 0.0)+0.1); item.last_duration_seconds=duration_seconds; item.last_new_jobs=new_jobs; item.last_duplicates=duplicates; item.baseline_initialized=baseline or item.baseline_initialized; s.commit()
    def health_failure(self, source, error):
        with self.sessions() as s:
            item=s.get(SourceHealth,source) or SourceHealth(source=source); s.add(item)
            item.last_checked_at=datetime.now(timezone.utc)
            item.consecutive_failures=(item.consecutive_failures or 0)+1
            item.last_error=error; item.status='unhealthy'; item.health_score=max(0.0,(item.health_score if item.health_score is not None else 1.0)-0.15); s.commit()
    def reserve_baseline_alert(self, limit=5):
        with self.sessions() as s:
            state=s.get(AppState,'baseline_alert_count')
            if not state: state=AppState(key='baseline_alert_count',value='0'); s.add(state)
            count=int(state.value)
            if count>=limit: s.commit(); return False
            state.value=str(count+1); s.commit(); return True
    def reserve_brightdata_page_loads(self, source: str, requested: int, limit: int) -> bool:
        """Atomically reserve a monthly Bright Data page-load budget.

        A rejected reservation happens before a paid/free-quota-consuming scrape
        request.  Each source is isolated so record-based sources do not affect
        JobStreet's page-load guard.
        """
        if requested < 1 or limit < 1:
            return False
        month=datetime.now(timezone.utc).strftime('%Y-%m')
        key=f'brightdata_page_loads:{source}:{month}'
        with self.sessions() as s:
            state=s.get(AppState,key)
            if not state:
                state=AppState(key=key,value='0'); s.add(state)
            used=int(state.value)
            if used+requested > limit:
                s.commit(); return False
            state.value=str(used+requested); s.commit(); return True
    def expire_stale_jobs(self):
        # The active discovery policy deliberately retains dated postings for
        # 0-90 days. A listing rediscovered today is not "new", but a 31-90
        # day listing remains eligible at lower priority.
        cutoff=datetime.now(timezone.utc)-timedelta(days=90)
        with self.sessions() as s:
            rows=s.query(Job).filter(Job.date_posted.is_not(None),Job.date_posted<cutoff,Job.status.notin_([JobStatus.APPLIED.value,JobStatus.OFFER.value,JobStatus.REJECTED.value])).all()
            for job in rows:
                job.status=JobStatus.EXPIRED.value
                job.is_active=False
            s.commit(); return len(rows)
    def by_fingerprint(self, fingerprint: str) -> Job | None:
        with self.sessions() as s:
            return s.scalar(select(Job).where(Job.fingerprint==fingerprint)) or s.scalar(select(Job).where(Job.identity_key==fingerprint))
    def by_item(self, item: NormalizedJob) -> Job | None:
        with self.sessions() as s:
            candidates=s.scalars(select(Job).where(Job.identity_key==self._identity_key(item))).all()
            if not candidates:
                candidates=s.scalars(select(Job).where(Job.fingerprint==item.fingerprint)).all()
            return next((candidate for candidate in candidates if self._same_vacancy(candidate,item)), None)
    def state(self, key: str, default=None):
        with self.sessions() as s:
            item=s.get(AppState,key)
            if not item: return default
            try: return json.loads(item.value)
            except json.JSONDecodeError: return default
    def set_state(self, key: str, value) -> None:
        with self.sessions() as s:
            item=s.get(AppState,key)
            encoded=json.dumps(value,default=str)
            if item: item.value=encoded
            else: s.add(AppState(key=key,value=encoded))
            s.commit()
    def acquire_discord_bot_lease(self, instance_id: str, ttl_seconds: int = 60) -> bool:
        """Acquire the renewable gateway lease without ever storing a token.

        Row locking makes this atomic on Postgres; SQLite serializes the small
        write transaction used by local tests/development.
        """
        key='discord_bot_gateway_lease'; now=datetime.now(timezone.utc)
        expires=now+timedelta(seconds=max(15, ttl_seconds))
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            current={}
            if row:
                try: current=json.loads(row.value)
                except json.JSONDecodeError: current={}
            owner=str(current.get('owner') or '')
            try:
                valid_until=datetime.fromisoformat(str(current.get('expires_at')))
                if valid_until.tzinfo is None: valid_until=valid_until.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError): valid_until=now-timedelta(seconds=1)
            if owner and owner != instance_id and valid_until > now:
                return False
            payload={'owner':instance_id,'expires_at':expires.isoformat()}
            if row: row.value=json.dumps(payload)
            else: s.add(AppState(key=key,value=json.dumps(payload)))
        return True
    def renew_discord_bot_lease(self, instance_id: str, ttl_seconds: int = 60) -> bool:
        key='discord_bot_gateway_lease'; now=datetime.now(timezone.utc)
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return False
            try: current=json.loads(row.value)
            except json.JSONDecodeError: return False
            if current.get('owner') != instance_id: return False
            row.value=json.dumps({'owner':instance_id,'expires_at':(now+timedelta(seconds=max(15,ttl_seconds))).isoformat()})
        return True
    def release_discord_bot_lease(self, instance_id: str) -> None:
        key='discord_bot_gateway_lease'
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return
            try: current=json.loads(row.value)
            except json.JSONDecodeError: current={}
            if current.get('owner') == instance_id:
                row.value=json.dumps({'owner':'','expires_at':datetime.now(timezone.utc).isoformat()})

    def acquire_worker_lease(self, instance_id: str, ttl_seconds: int = 120) -> bool:
        """Prevent duplicate polling when the host runs multiple web workers."""
        key='discovery_worker_lease'; now=datetime.now(timezone.utc); expires=now+timedelta(seconds=max(30,ttl_seconds))
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            current={}
            if row:
                try: current=json.loads(row.value)
                except json.JSONDecodeError: current={}
            try:
                valid_until=datetime.fromisoformat(str(current.get('expires_at')))
                if valid_until.tzinfo is None: valid_until=valid_until.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError): valid_until=now-timedelta(seconds=1)
            if current.get('owner') not in {None,'',instance_id} and valid_until > now:
                return False
            payload=json.dumps({'owner':instance_id,'expires_at':expires.isoformat()})
            if row: row.value=payload
            else: s.add(AppState(key=key,value=payload))
            return True

    def renew_worker_lease(self, instance_id: str, ttl_seconds: int = 120) -> bool:
        key='discovery_worker_lease'; now=datetime.now(timezone.utc)
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return False
            try: current=json.loads(row.value)
            except json.JSONDecodeError: return False
            if current.get('owner') != instance_id: return False
            row.value=json.dumps({'owner':instance_id,'expires_at':(now+timedelta(seconds=max(30,ttl_seconds))).isoformat()})
            return True

    def release_worker_lease(self, instance_id: str) -> None:
        key='discovery_worker_lease'
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return
            try: current=json.loads(row.value)
            except json.JSONDecodeError: current={}
            if current.get('owner') == instance_id:
                row.value=json.dumps({'owner':'','expires_at':datetime.now(timezone.utc).isoformat()})
    def claim_bot_alert(self, job_id: int) -> bool:
        """Atomically reserve a bot alert without ever re-claiming a sent one.

        ``FAILED`` is deliberately retryable: it means a prior *claimed* bot
        delivery did not complete.  ``SENT`` and ``BOT_SENDING`` are not, so a
        retry worker cannot turn one successful alert into a duplicate ping.
        """
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job or job.notification_state not in {'BOT_PENDING','FAILED'} or job.status in {JobStatus.SAVED.value,JobStatus.IGNORED.value,JobStatus.APPLIED.value,JobStatus.REJECTED.value}:
                return False
            job.notification_state='BOT_SENDING'
            job.notification_attempts=(job.notification_attempts or 0)+1
            job.last_notification_attempt=datetime.now(timezone.utc)
        return True

    def claim_webhook_alert(self, job_id: int) -> bool:
        """Atomically claim one webhook delivery across workers/digests."""
        with self.sessions.begin() as s:
            job=s.execute(select(Job).where(Job.id==job_id).with_for_update()).scalar_one_or_none()
            if not job or job.notification_state not in {'PENDING','FAILED'}:
                return False
            job.notification_state='WEBHOOK_SENDING'
            job.notification_attempts=(job.notification_attempts or 0)+1
            job.last_notification_attempt=datetime.now(timezone.utc)
            return True

    def recover_stuck_notifications(self, stale_seconds: int = 900) -> int:
        """Return bot claims stranded by a process restart to the retry queue."""
        cutoff=datetime.now(timezone.utc)-timedelta(seconds=max(60, stale_seconds))
        with self.sessions.begin() as s:
            rows=s.scalars(select(Job).where(Job.notification_state.in_({'BOT_SENDING','WEBHOOK_SENDING'}), (Job.last_notification_attempt.is_(None) | (Job.last_notification_attempt < cutoff)))).all()
            for job in rows:
                job.notification_state='FAILED'
                job.notification_next_attempt_at=datetime.now(timezone.utc)
                job.notification_error='delivery claim recovered after worker restart'
            return len(rows)

    def due_notification_ids(self, minimum_score: int, limit: int = 500, max_attempts: int | None = None) -> list[int]:
        now=datetime.now(timezone.utc)
        with self.sessions() as s:
            query=select(Job.id).where(
                Job.score >= minimum_score,
                Job.status.not_in((JobStatus.EXPIRED.value,JobStatus.IGNORED.value,JobStatus.APPLIED.value,JobStatus.REJECTED.value)),
                Job.notification_state.in_(('PENDING','FAILED','BOT_PENDING')),
                (Job.notification_next_attempt_at.is_(None) | (Job.notification_next_attempt_at <= now)),
            )
            if max_attempts is not None:
                query=query.where(Job.notification_attempts < max(1,max_attempts))
            return s.scalars(query.order_by(Job.score.desc(),Job.posted_at.desc().nullslast()).limit(max(1,limit))).all()

    def record_notification_attempt(self, job_id: int, state: str, error: str | None = None, next_attempt_at: datetime | None = None, increment: bool = True) -> bool:
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job:
                return False
            if increment:
                job.notification_attempts=(job.notification_attempts or 0)+1
            job.last_notification_attempt=datetime.now(timezone.utc)
            job.notification_state=state
            job.notification_error=error
            job.notification_next_attempt_at=next_attempt_at
            return True

    def record_alert_delivery(self, job_id: int, notification_state: str) -> bool:
        """Persist delivery state without replacing a user/application decision.

        Both the webhook path and the Discord-bot path can use this one small
        transaction.  The status transition to ``NOTIFIED`` is conditional on
        the current database value instead of the detached ``Job`` object that
        was used to render the alert, which closes a save/skip/apply race.
        """
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job:
                return False
            job.notification_state=notification_state
            job.last_notification_attempt=datetime.now(timezone.utc)
            job.notification_error=None if notification_state == 'SENT' else job.notification_error
            job.notification_next_attempt_at=None if notification_state in {'SENT','SKIPPED'} else job.notification_next_attempt_at
            if notification_state == 'SENT' and job.status in NOTIFICATION_MUTABLE_STATUSES:
                job.status=JobStatus.NOTIFIED.value
        return True
    def set_job_status(self, job_id: int, status: str, detail: str = '') -> bool:
        """Persist a state change and its audit event in one transaction."""
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job: return False
            if job.status == status: return True
            previous=job.status; job.status=status
            s.add(JobEvent(job_id=job_id,event_type=status,detail=detail or f'{previous} → {status}'))
        return True
    def record_job_event(self, job_id: int, event_type: str, detail: str = '') -> None:
        with self.sessions.begin() as s:
            if s.get(Job,job_id): s.add(JobEvent(job_id=job_id,event_type=event_type,detail=detail))
    def resume_record(self):
        with self.sessions() as s:
            item=s.get(ResumeProfile,1)
            if not item: return None
            return {'filename':item.filename,'content_type':item.content_type,'file_data':item.file_data,'extracted_text':item.extracted_text,'uploaded_at':item.uploaded_at}
    def resume_info(self):
        item=self.resume_record()
        if not item: return None
        return {key:item[key] for key in ('filename','content_type','uploaded_at')}
    def resume_text(self):
        item=self.resume_record()
        return item['extracted_text'] if item else None
    def save_resume(self, filename: str, content_type: str, file_data: bytes, extracted_text: str):
        with self.sessions() as s:
            item=s.get(ResumeProfile,1)
            if not item:
                item=ResumeProfile(id=1,filename=filename,content_type=content_type,file_data=file_data,extracted_text=extracted_text)
                s.add(item)
            else:
                item.filename=filename; item.content_type=content_type; item.file_data=file_data; item.extracted_text=extracted_text; item.uploaded_at=datetime.now(timezone.utc)
            for job in s.scalars(select(Job)).all():
                metadata=dict(job.raw_metadata or {})
                for key in (
                    'cover_letter', 'cover_letter_mode', 'cover_letter_generation_method',
                    'cover_letter_failure_reason', 'cover_letter_similarity',
                    'cover_letter_version', 'cover_letter_body_hash', 'cover_letter_versions',
                ):
                    metadata.pop(key, None)
                job.raw_metadata=metadata
            s.commit()
            return self.resume_info()
class Discord:
    def __init__(self,url:str|None, motivations:list[str]|str='', cfg:Settings|None=None):
        configured=motivations if isinstance(motivations,list) else [motivations]
        self.url=url; self.motivations=[x for x in configured if x] or ['']; self.cfg=cfg
    def _link(self,job,action):
        if not self.cfg or not self.cfg.public_base_url: return None
        token=ActionTokens(self.cfg).issue(job.id,action)
        return f'{self.cfg.public_base_url.rstrip("/")}/actions/{token}' if token else None
    @staticmethod
    def _posted_label(value):
        if not value: return None
        today=datetime.now(ZoneInfo('Asia/Manila')).date(); posted=value.astimezone(ZoneInfo('Asia/Manila')).date(); days=(today-posted).days
        if days<=0: return 'Posted: Today'
        if days==1: return 'Posted: Yesterday'
        if days<=14: return f'Posted: {days} days ago'
        return f'Posted: {posted.strftime("%b %d, %Y")}'
    def payload(self, job: Job, test=False):
        label='𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇'
        details=[job.location or 'Location not stated']
        label='EXCELLENT MATCH' if job.score>=90 else 'STRONG MATCH' if job.score>=80 else 'GOOD MATCH' if job.score>=70 else 'STRETCH - CONSIDER APPLYING' if job.score>=60 else 'LOW PRIORITY'
        if job.work_setup and job.work_setup.lower() not in (job.location or '').lower(): details.append(job.work_setup)
        if job.salary: details.append(job.salary)
        if job.date_posted: details.append(self._posted_label(job.date_posted))
        match='\n'.join(job.match_reasons[:6]) or 'Entry-level technology role'
        fields=[{'name':'𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒','value':match,'inline':False}]
        if job.warnings: fields.append({'name':'𝐍𝐎𝐓𝐄𝐒','value':'\n'.join(job.warnings[:2]),'inline':False})
        if job.experience_min is not None or job.experience_max is not None:
            lower='0' if job.experience_min is None else f'{job.experience_min:g}'
            upper='+' if job.experience_max is None else f'-{job.experience_max:g}'
            fields.append({'name':'EXPERIENCE','value':f'{lower}{upper} years','inline':True})
        if job.skills:
            fields.append({'name':'IMPORTANT TECHNOLOGIES','value':', '.join(job.skills[:10]),'inline':False})
        row1=[{'type':2,'style':5,'label':'VIEW JOB','url':job.url}]
        if job.application_url and job.application_url!=job.url and job.application_url.startswith(('https://','http://')):
            row1.append({'type':2,'style':5,'label':'DIRECT APPLY','url':job.application_url})
        review=self._link(job,'review')
        if review and job.status not in ('APPLIED','IGNORED'):
            row1 += [{'type':2,'style':5,'label':'APPLY NOW','url':review}]
        rows=[{'type':1,'components':row1}]
        actions=[('REVIEW APPLICATION','review'),('SAVE','saved'),('SKIP','ignored')]
        row2=[{'type':2,'style':5,'label':label,'url':url} for label,action in actions if (url:=self._link(job,action))]
        if row2: rows.append({'type':1,'components':row2})
        kind,name=(job.source.split(':',1)+[''])[:2] if ':' in job.source else (job.source,'')
        footer=f'{kind.replace("_"," ").title()}' + (f' · {name}' if name else '')
        if test: footer='𝐓𝐄𝐒𝐓 𝐀𝐋𝐄𝐑𝐓 — No real application will be sent.'
        fields.append({'name':'𝐒𝐓𝐀𝐓𝐔𝐒','value':'Ready to review' if job.status not in ('APPLIED','IGNORED') else job.status.title(),'inline':False})
        if self.cfg and self.cfg.public_base_url:
            fields.append({'name':'𝐖𝐀𝐍𝐓 𝐓𝐎 𝐅𝐈𝐍𝐃 𝐀 𝐒𝐏𝐄𝐂𝐈𝐅𝐈𝐂 𝐉𝐎𝐁?','value':'Find or filter the exact role you want to apply for here.','inline':False})
        configured_role=(self.cfg.discord_alert_role_id if self.cfg else None)
        role_id=configured_role if configured_role in DISCORD_ALERT_ROLE_ALLOWLIST else None
        role_mention=f'<@&{role_id}>' if role_id and not test else ''
        content='\n\n'.join(part for part in (role_mention,random.choice(self.motivations)) if part)
        allowed={'parse':[], 'roles':[str(role_id)]} if role_id and not test else {'parse':[]}
        return {'content':content,'allowed_mentions':allowed,'embeds':[{'title':label,'description':f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n\n'+' · '.join(details),'url':job.url,'fields':fields,'footer':{'text':footer}}],'components':[] if test else rows}
    def digest_payload(self, jobs: list[Job]) -> dict:
        """Build a no-ping multi-embed payload; Discord allows at most ten embeds."""
        embeds=[]
        for job in jobs[:10]:
            embeds.extend(self.payload(job,test=True).get('embeds',[]))
        return {'content':f'JOB DIGEST · {len(embeds)} matching openings','allowed_mentions':{'parse':[]},'embeds':embeds}
    async def send(self,job):
        if not self.url: return 'SKIPPED'
        return await self.send_payload(self.payload(job))
    async def send_payload(self,payload):
        if not self.url: return 'SKIPPED'
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.post(self.url,params={'with_components':'true'},json=payload); r.raise_for_status()
        return 'SENT'
    async def update_status(self, repo: Repository, scheduler_state: dict):
        """Create one permanent control panel, then edit it after each cycle."""
        if not self.url: return 'SKIPPED'
        def stamp(value):
            if not value: return '—'
            return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Manila')).strftime('%I:%M %p')
        sources=scheduler_state.get('sources_working',0); linked=scheduler_state.get('linkedin_status','DISABLED'); indeed=scheduler_state.get('indeed_status','STARTING'); google=scheduler_state.get('google_jobs_status','STARTING'); jobstreet=scheduler_state.get('jobstreet_status','DISABLED'); phase=scheduler_state.get('phase','complete')
        system=f"Service\nONLINE\n\nScheduler\n{scheduler_state.get('status','RUNNING').upper()}\n\nLast scan\n{stamp(scheduler_state.get('last_poll_at'))}\n\nNext scan\n{stamp(scheduler_state.get('next_poll_at'))}\n\nNext batch\n{max(0,scheduler_state.get('seconds_until_next_poll',0))//60} minutes"
        sources_text=f"Sources\n{sources} active\n\nIndeed PH\n{indeed}\n\nGoogle Jobs\n{google}\n\nLinkedIn\n{linked}\n\nJobStreet\n{jobstreet}\n\nDatabase\nCONNECTED\n\nDiscord\nCONNECTED"
        scan_text='Checking recent active jobs…' if phase=='scanning' else f"Jobs checked: {scheduler_state.get('jobs_checked',0)}\nRecent PH tech jobs: {scheduler_state.get('recent_jobs_found',0)}\nNew qualifying jobs: {scheduler_state.get('new_recent_jobs',0)}\nAlerts sent: {scheduler_state.get('alerts_sent',0)}\nDuplicates ignored: {scheduler_state.get('duplicates_ignored',0)}"
        payload={'embeds':[{'title':'𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑','description':'Your automated Philippine tech-job monitor is online.','color':0xF59E0B,'fields':[{'name':'𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒','value':system,'inline':True},{'name':'𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒','value':sources_text,'inline':True},{'name':'𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if phase=='scanning' else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄','value':scan_text,'inline':False},{'name':'𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','value':'Auto scanning runs every 15 minutes. Use v!search for a specific role, v!latest for stored jobs, v!status for health, or v!scan for one protected immediate batch.','inline':False}],'footer':{'text':'After Hours Job Hunter • Made by masoncalix'}}]}
        existing=repo.state('discord_control_panel_message_id') or repo.state('discord_status_message_id')
        async with httpx.AsyncClient(timeout=15) as client:
            # Remove the legacy separate welcome message once during migration;
            # the control panel now owns both status and tutorial content.
            legacy=repo.state('discord_welcome_message_id')
            if legacy:
                removed=await client.delete(f'{self.url}/messages/{legacy}')
                if removed.status_code in (204,404): repo.set_state('discord_welcome_message_id',None)
            if existing:
                response=await client.patch(f'{self.url}/messages/{existing}',params={'with_components':'true'},json=payload)
                if response.status_code==404: existing=None
                else: response.raise_for_status(); return 'UPDATED'
            response=await client.post(self.url,params={'wait':'true','with_components':'true'},json=payload); response.raise_for_status()
            message_id=response.json().get('id')
            if message_id: repo.set_state('discord_control_panel_message_id',message_id)
        return 'CREATED'
    async def remove_webhook_control_panel(self, repo: Repository):
        """Migrate from webhook link components to the bot-owned panel."""
        if not self.url: return
        ids=[repo.state('discord_control_panel_message_id'),repo.state('discord_status_message_id'),repo.state('discord_welcome_message_id')]
        async with httpx.AsyncClient(timeout=15) as client:
            for message_id in {x for x in ids if x}:
                response=await client.delete(f'{self.url}/messages/{message_id}')
                if response.status_code not in (204,404): response.raise_for_status()
        repo.set_state('discord_control_panel_message_id',None); repo.set_state('discord_status_message_id',None); repo.set_state('discord_welcome_message_id',None)
class Pipeline:
    def __init__(self, repo:Repository, config:Settings): self.repo=repo; self.config=config; self.discord=Discord(config.discord_webhook_url,config.discord_motivations,config); self._baseline_lock=asyncio.Lock(); self._cycle_lock=asyncio.Lock(); self._source_discovery_lock=asyncio.Lock(); self._discovered_source_cache=set(); self._cycle_notifications=0; self._cycle_delivered=0
    def begin_cycle(self): self._cycle_notifications=0; self._cycle_delivered=0
    async def process(self, item:NormalizedJob, notify=True) -> tuple[Job|None,bool]:
        enrich_canonical(item)
        # Broad public providers often expose an official ATS application URL.
        # Promote that URL once into the durable source registry so future
        # cycles can poll the first-party board directly.
        from .discovery import target_from_job_url
        for candidate in (item.application_url,item.url,(item.raw_metadata or {}).get('canonical_url')):
            target=target_from_job_url(candidate,item.company)
            if not target: continue
            key=(target.get('kind'),target.get('token') or target.get('site') or target.get('board') or target.get('company'))
            async with self._source_discovery_lock:
                if key not in self._discovered_source_cache:
                    await asyncio.to_thread(self.repo.upsert_source_target,target,'job_outbound_link')
                    self._discovered_source_cache.add(key)
            break
        if not is_ph_location(item) or not is_active_listing(item): return None, False
        score,reasons,warnings,relevant=evaluate(item)
        _,_,fresh=freshness(item)
        # Jobs remain eligible throughout the 90-day window when the listing
        # is active and the role is technically relevant.  Freshness affects
        # ranking and alerts, but age alone must not discard a 31–90 day job.
        if not relevant or not fresh: return None, False
        job,outcome=await asyncio.to_thread(self.repo.save_with_result,item,score,reasons,warnings)
        item.raw_metadata=dict(item.raw_metadata or {})
        item.raw_metadata['_pipeline_save_outcome']=outcome
        if not job: return None, True
        if notify and score>=self.config.instant_alert_score: await self.notify(job)
        return job, True
    async def _deliver_webhook(self, job: Job) -> str:
        if not await asyncio.to_thread(self.repo.claim_webhook_alert,job.id):
            return 'SKIPPED'
        try:
            result=await self.discord.send(job)
            state='SENT' if result == 'SENT' else 'SKIPPED'
            await asyncio.to_thread(self.repo.record_alert_delivery,job.id,state)
            if state == 'SENT': self._cycle_delivered+=1
            return state
        except Exception as error:
            attempts=(job.notification_attempts or 0)+1
            retry_after=None
            response=getattr(error,'response',None)
            if response is not None:
                value=response.headers.get('Retry-After')
                try: retry_after=float(value) if value else None
                except ValueError: retry_after=None
            delay=max(retry_after or 0,min(3600,self.config.notification_retry_base_seconds*(2**max(0,attempts-1))))
            exhausted=attempts>=self.config.notification_retry_max_attempts
            next_attempt=None if exhausted else datetime.now(timezone.utc)+timedelta(seconds=delay)
            await asyncio.to_thread(self.repo.record_notification_attempt,job.id,'FAILED',str(error)[:500],next_attempt,False)
            log.warning('discord_notification_failed',extra={'job_id':job.id,'attempt':attempts,'retry_exhausted':exhausted,'error':type(error).__name__})
            return 'FAILED'
    async def notify(self, job: Job, force: bool = False):
        async with self._cycle_lock:
            if not force and self._cycle_notifications>=self.config.max_notifications_per_cycle: return
            self._cycle_notifications+=1
        bot_state=await asyncio.to_thread(self.repo.state,'discord_bot_health',{}) or {}
        # Before the first successful bot connection, hold alerts for the bot
        # instead of racing a startup webhook. Once a known bot disconnects,
        # the webhook is the emergency fallback.
        if self.config.discord_bot_token and (bot_state.get('healthy') or not bot_state.get('started_once')):
            job.notification_state='BOT_PENDING'
            def hold_for_bot():
                with self.repo.sessions() as s:
                    stored=s.get(Job,job.id)
                    if stored:
                        stored.notification_state='BOT_PENDING'; stored.notification_error=None; stored.notification_next_attempt_at=None; s.commit()
            await asyncio.to_thread(hold_for_bot)
            return
        if not self.config.discord_webhook_url:
            job.notification_state='SKIPPED'
            await asyncio.to_thread(self.repo.record_alert_delivery,job.id,'SKIPPED')
            return
        job.notification_state=await self._deliver_webhook(job)
    async def retry_notifications(self):
        await asyncio.to_thread(self.repo.recover_stuck_notifications)
        if not self.config.discord_webhook_url: return 0
        ids=await asyncio.to_thread(self.repo.due_notification_ids,self.config.instant_alert_score,500,self.config.notification_retry_max_attempts)
        for job_id in ids:
            def load_job():
                with self.repo.sessions() as s: return s.get(Job,job_id)
            job=await asyncio.to_thread(load_job)
            if job and job.notification_state=='FAILED': await self._deliver_webhook(job)
        return len(ids)

    async def send_digest(self) -> int:
        """Deliver all due good/stretch matches in bounded Discord batches."""
        if not self.config.digest_enabled or not self.config.discord_webhook_url:
            return 0
        last=self.repo.state('last_digest_at')
        if last:
            try:
                if (datetime.now(timezone.utc)-datetime.fromisoformat(str(last))).total_seconds() < self.config.digest_interval_seconds:
                    return 0
            except ValueError:
                pass
        ids=await asyncio.to_thread(self.repo.due_notification_ids,self.config.min_notify_score,5000,self.config.notification_retry_max_attempts)
        if not ids:
            return 0
        jobs=[]
        for job_id in ids:
            def load(job_id=job_id):
                with self.repo.sessions() as s: return s.get(Job,job_id)
            job=await asyncio.to_thread(load)
            if job and job.notification_state in {'PENDING','FAILED'}:
                jobs.append(job)
        delivered=0
        for start in range(0,len(jobs),max(1,min(10,self.config.notification_batch_size))):
            candidates=jobs[start:start+max(1,min(10,self.config.notification_batch_size))]
            batch=[]
            for job in candidates:
                if await asyncio.to_thread(self.repo.claim_webhook_alert,job.id):
                    batch.append(job)
            if not batch:
                continue
            payload=self.discord.digest_payload(batch)
            try:
                await self.discord.send_payload(payload)
            except Exception as error:
                for job in batch:
                    attempts=(job.notification_attempts or 0)+1
                    delay=min(3600,self.config.notification_retry_base_seconds*(2**max(0,attempts-1)))
                    next_attempt=None if attempts>=self.config.notification_retry_max_attempts else datetime.now(timezone.utc)+timedelta(seconds=delay)
                    await asyncio.to_thread(self.repo.record_notification_attempt,job.id,'FAILED',str(error)[:500],next_attempt,False)
                continue
            for job in batch:
                await asyncio.to_thread(self.repo.record_alert_delivery,job.id,'SENT')
                delivered+=1
        if delivered:
            self.repo.set_state('last_digest_at',datetime.now(timezone.utc).isoformat())
        return delivered
    async def run_source(self,source):
        run=await asyncio.to_thread(self.repo.run_start,source.name); started=time.monotonic(); discovered=new=filtered=0
        pages_fetched=0
        raw_jobs_discovered=normalized_jobs=jobs_0_90=ph_remote_ph=computer_related=entry_level_compatible=qualifying=duplicates_removed=malformed_jobs=jobs_updated=0
        succeeded=False
        try:
            max_attempts=max(1,int(getattr(source,'max_fetch_attempts',3) or 3))
            for attempt in range(max_attempts):
                try: items=await source.fetch(); break
                except Exception:
                    if attempt==max_attempts-1: raise
                    await asyncio.sleep(2**attempt)
            pages_fetched=int(getattr(source,'pages_fetched',1) or 1)
            raw_jobs_discovered=int(getattr(source,'raw_jobs_discovered',len(items)) or 0)
            normalized_jobs=int(getattr(source,'normalized_jobs',len(items)) or 0)
            malformed_jobs+=int(getattr(source,'normalization_errors',0) or 0)
            now=datetime.now(timezone.utc)
            for item in items:
                try:
                    enrich_canonical(item)
                    if not is_active_listing(item):
                        continue
                    _,_,in_window=freshness(item,now)
                    if not in_window:
                        continue
                    jobs_0_90 += 1
                    if not is_ph_location(item):
                        continue
                    ph_remote_ph += 1
                    score,_,warnings,_profile_eligible=evaluate(item,now)
                    # "Tech related" precedes the seniority stage in the
                    # observable funnel. A Senior DevOps role is still technical;
                    # it is counted here and removed only from entry-level
                    # compatibility/qualification below.
                    technical=not any(warning.startswith('Role is outside') for warning in warnings)
                    if not technical:
                        continue
                    computer_related += 1
                    if not any(
                        warning.startswith('Senior-level') or warning.startswith('Requires 5+')
                        for warning in warnings
                    ) and score >= 0:
                        entry_level_compatible += 1
                except Exception as exc:
                    malformed_jobs += 1
                    log.warning('malformed_job_skipped',extra={'source':source.name,'error_type':type(exc).__name__})
            seen_fingerprints=set(); unique_items=[]
            for item in items:
                try:
                    if item.dedup_token in seen_fingerprints:
                        duplicates_removed += 1
                        continue
                    seen_fingerprints.add(item.dedup_token)
                    unique_items.append(item)
                except Exception as exc:
                    malformed_jobs += 1
                    log.warning('malformed_job_dedup_skipped',extra={'source':source.name,'error_type':type(exc).__name__})
            health=await asyncio.to_thread(self.repo.health,source.name)
            baseline=not health.baseline_initialized
            def ranking(item):
                try: return evaluate(item)[0]
                except Exception: return -1
            ordered=sorted(unique_items,key=ranking,reverse=True)
            baseline_candidates=[]
            for item in ordered:
                discovered+=1
                try:
                    job,accepted=await self.process(item,notify=not baseline)
                    save_outcome=(item.raw_metadata or {}).pop('_pipeline_save_outcome',None)
                    filtered+=not accepted; new+=int(save_outcome in {'new','reposted'})
                    jobs_updated+=int(save_outcome=='updated')
                    qualifying += int(accepted)
                    if accepted and save_outcome=='duplicate':
                        duplicates_removed += 1
                    if baseline and job and job.score>=self.config.min_notify_score: baseline_candidates.append(job)
                except Exception as exc:
                    malformed_jobs += 1; filtered += 1
                    log.warning('job_processing_failed',extra={'source':source.name,'error_type':type(exc).__name__})
            if baseline:
                for job in baseline_candidates:
                    async with self._baseline_lock:
                        allowed=await asyncio.to_thread(self.repo.reserve_baseline_alert)
                    if not allowed: break
                    await self.notify(job)
            duration=time.monotonic()-started
            await asyncio.to_thread(self.repo.run_finish,run,success=True,status='SUCCESS',duration_seconds=duration,discovered=discovered,new_jobs=new,jobs_updated=jobs_updated,filtered=filtered,duplicates=duplicates_removed)
            status='degraded' if getattr(source,'degraded',False) else 'healthy'
            await asyncio.to_thread(
                self.repo.health_success, source.name, discovered, baseline=True,
                status=status, error=getattr(source,'last_error_category',None),
                raw_jobs=raw_jobs_discovered, normalized_jobs=normalized_jobs,
                accepted_jobs=qualifying,duration_seconds=duration,new_jobs=new,duplicates=duplicates_removed,
            )
            await asyncio.to_thread(self.repo.record_source_registry_result,source.name,True,None)
            if source.name.startswith('jobspy:'):
                log.info('jobspy_source_funnel',extra={'source':source.name,'raw_jobs':raw_jobs_discovered,'normalized_jobs':normalized_jobs,'jobs_0_90':jobs_0_90,'ph_remote_ph':ph_remote_ph,'computer_related':computer_related,'entry_level_compatible':entry_level_compatible,'duplicates':duplicates_removed,'new_jobs':new,'qualifying':qualifying,'status':status})
            log.info('source_run_complete',extra={'source':source.name,'status':status,'raw_jobs':raw_jobs_discovered,'normalized_jobs':normalized_jobs,'accepted_jobs':qualifying,'new_jobs':new,'jobs_updated':jobs_updated,'duplicates':duplicates_removed,'malformed_jobs':malformed_jobs,'duration_seconds':round(duration,3)})
            succeeded=True
        except asyncio.CancelledError:
            duration=time.monotonic()-started
            message='source run cancelled by scheduler timeout or shutdown'
            await asyncio.to_thread(self.repo.run_finish,run,success=False,status='TIMEOUT',duration_seconds=duration,error=message)
            await asyncio.to_thread(self.repo.health_failure,source.name,message)
            await asyncio.to_thread(self.repo.record_source_registry_result,source.name,False,message)
            raise
        except Exception as e:
            duration=time.monotonic()-started
            await asyncio.to_thread(self.repo.run_finish,run,success=False,status='FAILED',duration_seconds=duration,error=str(e)); log.warning('source_failed',extra={'source':source.name,'error':str(e),'duration_seconds':round(duration,2)})
            await asyncio.to_thread(self.repo.health_failure,source.name,str(e))
            await asyncio.to_thread(self.repo.record_source_registry_result,source.name,False,str(e))
        return {
            'source':source.name,'discovered':discovered,'new':new,'filtered':filtered,
            'success': succeeded,'pages_fetched':pages_fetched,'raw_jobs_discovered':raw_jobs_discovered,
            'normalized_jobs':normalized_jobs,'jobs_0_90':jobs_0_90,'ph_remote_ph':ph_remote_ph,
            'computer_related':computer_related,'entry_level_compatible':entry_level_compatible,
            'qualifying':qualifying,
            'duplicates_removed':duplicates_removed,
            'malformed_jobs':malformed_jobs,
            'jobs_updated':jobs_updated,
        }
