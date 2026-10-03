"""Supabase PostgREST Client for cloud sync with Supabase Postgres."""

from __future__ import annotations

import logging
from typing import Any
import httpx

from radar import config

logger = logging.getLogger("radar.supabase")


class SupabaseClient:
    """Direct HTTP PostgREST client for Supabase without requiring external client library."""

    def __init__(
        self,
        base_url: str | None = None,
        service_key: str | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.base_url = (base_url or config.SUPABASE_URL).rstrip("/")
        self.key = service_key or config.SUPABASE_SERVICE_ROLE_KEY or config.SUPABASE_ANON_KEY
        self.timeout_s = timeout_s

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.key and "supabase.co" in self.base_url)

    def _headers(self, prefer_return: bool = True) -> dict[str, str]:
        h = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer_return:
            h["Prefer"] = "return=representation"
        return h

    def check_connection(self) -> tuple[bool, str]:
        """Verify whether Supabase project is active or paused."""
        if not self.is_configured:
            return False, "Supabase credentials not configured in .env"
        try:
            url = f"{self.base_url}/rest/v1/"
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    return True, "Connected to Supabase PostgREST"
                if res.status_code == 521 or "server is down" in res.text.lower():
                    return False, "Supabase project is paused on free tier (Cloudflare 521). Restore in Supabase dashboard."
                return False, f"Supabase returned status {res.status_code}: {res.text[:120]}"
        except Exception as exc:
            return False, f"Connection error: {exc}"

    def insert_job(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Insert a job row into Supabase public.jobs table."""
        if not self.is_configured:
            return None
        url = f"{self.base_url}/rest/v1/jobs"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.post(url, json=data, headers=self._headers(prefer_return=True))
                if res.status_code in (200, 201):
                    rows = res.json()
                    return rows[0] if isinstance(rows, list) and rows else rows
                if res.status_code == 400 and "user_id" in res.text and "user_id" in data:
                    fallback_data = {k: v for k, v in data.items() if k != "user_id"}
                    res2 = client.post(url, json=fallback_data, headers=self._headers(prefer_return=True))
                    if res2.status_code in (200, 201):
                        rows = res2.json()
                        return rows[0] if isinstance(rows, list) and rows else rows
                logger.warning(f"Supabase job insert returned {res.status_code}: {res.text[:150]}")
        except Exception as e:
            logger.warning(f"Supabase job insert skipped (network/pause): {e}")
        return None

    def get_job_by_dedup_key(self, dedup_key: str) -> dict[str, Any] | None:
        """Look up a job row in Supabase by dedup_key."""
        if not self.is_configured or not dedup_key:
            return None
        url = f"{self.base_url}/rest/v1/jobs?dedup_key=eq.{dedup_key}&select=id,title,status"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data[0] if data and isinstance(data, list) else None
        except Exception:
            pass
        return None

    def get_job_by_id(self, job_id: int) -> dict[str, Any] | None:
        """Look up a job row in Supabase by its primary key ID."""
        if not self.is_configured or not job_id:
            return None
        url = f"{self.base_url}/rest/v1/jobs?id=eq.{job_id}&select=*"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data[0] if data and isinstance(data, list) else None
        except Exception:
            pass
        return None

    def upsert_application(self, data: dict[str, Any], session: Any = None) -> dict[str, Any] | None:
        """Upsert an application row into Supabase public.applications table.
        
        Safely maps local job_id to Supabase job_id via dedup_key to prevent
        foreign key constraint violations, and computes the next ID if absent to
        prevent sequence collision conflicts.
        """
        if not self.is_configured:
            return None

        clean_data = {k: v for k, v in data.items() if v is not None}
        if not clean_data.get("email"):
            clean_data["email"] = "portal-application@careerpulse.internal"

        # Resolve foreign key: job_id
        local_job_id = clean_data.get("job_id")
        if local_job_id and session:
            try:
                from app.models import Job
                job_row = session.get(Job, local_job_id)
                sb_job_id = None
                if job_row and job_row.dedup_key:
                    sb_job = self.get_job_by_dedup_key(job_row.dedup_key)
                    if sb_job:
                        sb_job_id = sb_job.get("id")
                clean_data["job_id"] = sb_job_id
            except Exception:
                clean_data["job_id"] = None
        elif local_job_id and not session:
            clean_data["job_id"] = None

        # Resolve foreign key: user_id
        local_user_id = clean_data.get("user_id")
        if local_user_id and session:
            try:
                from app.models import User
                user_row = session.get(User, local_user_id)
                sb_user_id = None
                if user_row and user_row.email:
                    sb_u = self.get_user_by_email(user_row.email)
                    if sb_u:
                        sb_user_id = sb_u.get("id")
                clean_data["user_id"] = sb_user_id
            except Exception:
                clean_data["user_id"] = None

        app_id = clean_data.get("id")
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                if app_id:
                    patch_url = f"{self.base_url}/rest/v1/applications?id=eq.{app_id}"
                    patch_res = client.patch(patch_url, json=clean_data, headers=self._headers(prefer_return=True))
                    if patch_res.status_code == 200:
                        rows = patch_res.json()
                        if rows:
                            return rows[0]

                # If no app_id or PATCH returned empty, ensure clean_data has a valid unique ID
                if not app_id:
                    m_res = client.get(f"{self.base_url}/rest/v1/applications?select=id&order=id.desc&limit=1", headers=self._headers(prefer_return=False))
                    if m_res.status_code == 200 and m_res.json():
                        clean_data["id"] = int(m_res.json()[0]["id"]) + 1
                    else:
                        clean_data["id"] = 1

                post_url = f"{self.base_url}/rest/v1/applications"
                headers = self._headers(prefer_return=True)
                headers["Prefer"] = "resolution=merge-duplicates,return=representation"
                post_res = client.post(post_url, json=clean_data, headers=headers)
                if post_res.status_code in (200, 201):
                    rows = post_res.json()
                    return rows[0] if isinstance(rows, list) and rows else rows
                logger.warning(f"Supabase application upsert returned {post_res.status_code}: {post_res.text[:150]}")
        except Exception as e:
            logger.warning(f"Supabase application upsert error: {e}")
        return None

    def insert_application(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Insert an application row into Supabase public.applications table (alias)."""
        return self.upsert_application(data)

    def fetch_all_applications(self, user_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """Fetch application records from Supabase public.applications table."""
        if not self.is_configured:
            return []
        query_params = ["select=*"]
        if user_id is not None:
            query_params.append(f"user_id=eq.{user_id}")
        if status is not None:
            if "," in status:
                query_params.append(f"status=in.({status})")
            else:
                query_params.append(f"status=eq.{status}")
        query_params.append("order=updated_at.desc")
        url = f"{self.base_url}/rest/v1/applications?{'&'.join(query_params)}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"Supabase fetch_all_applications error: {e}")
        return []

    def get_application_by_id(self, app_id: int) -> dict[str, Any] | None:
        """Fetch application by id from Supabase."""
        if not self.is_configured or not app_id:
            return None
        url = f"{self.base_url}/rest/v1/applications?id=eq.{app_id}&limit=1"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    rows = res.json()
                    if rows and isinstance(rows, list):
                        return rows[0]
        except Exception as e:
            logger.warning(f"Supabase get_application_by_id error: {e}")
        return None

    def fetch_all_users(self) -> list[dict[str, Any]]:
        """Fetch all users from Supabase."""
        if not self.is_configured:
            return []
        url = f"{self.base_url}/rest/v1/users?select=*&order=id.asc"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"Supabase fetch_all_users error: {e}")
        return []

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Fetch user by email from Supabase."""
        if not self.is_configured or not email:
            return None
        clean_email = email.strip().lower()
        url = f"{self.base_url}/rest/v1/users?email=eq.{clean_email}&limit=1"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    rows = res.json()
                    if rows and isinstance(rows, list):
                        return rows[0]
        except Exception as e:
            logger.warning(f"Supabase get_user_by_email error: {e}")
        return None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Fetch user by id from Supabase."""
        if not self.is_configured or not user_id:
            return None
        url = f"{self.base_url}/rest/v1/users?id=eq.{user_id}&limit=1"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    rows = res.json()
                    if rows and isinstance(rows, list):
                        return rows[0]
        except Exception as e:
            logger.warning(f"Supabase get_user_by_id error: {e}")
        return None

    def upsert_user(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Upsert a user row into Supabase."""
        if not self.is_configured:
            return None
        clean_data = {k: v for k, v in data.items() if v is not None}
        user_id = clean_data.get("id")
        user_email = clean_data.get("email")
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                if user_id:
                    patch_url = f"{self.base_url}/rest/v1/users?id=eq.{user_id}"
                    patch_res = client.patch(patch_url, json=clean_data, headers=self._headers(prefer_return=True))
                    if patch_res.status_code == 200 and patch_res.json():
                        return patch_res.json()[0]
                elif user_email:
                    patch_url = f"{self.base_url}/rest/v1/users?email=eq.{user_email}"
                    patch_res = client.patch(patch_url, json=clean_data, headers=self._headers(prefer_return=True))
                    if patch_res.status_code == 200 and patch_res.json():
                        return patch_res.json()[0]

                # If creating a new user, calculate next id if absent
                if not user_id:
                    m_res = client.get(f"{self.base_url}/rest/v1/users?select=id&order=id.desc&limit=1", headers=self._headers(prefer_return=False))
                    if m_res.status_code == 200 and m_res.json():
                        clean_data["id"] = int(m_res.json()[0]["id"]) + 1
                    else:
                        clean_data["id"] = 1

                post_url = f"{self.base_url}/rest/v1/users"
                headers = self._headers(prefer_return=True)
                headers["Prefer"] = "resolution=merge-duplicates,return=representation"
                post_res = client.post(post_url, json=clean_data, headers=headers)
                if post_res.status_code in (200, 201):
                    rows = post_res.json()
                    return rows[0] if isinstance(rows, list) and rows else rows
        except Exception as e:
            logger.warning(f"Supabase user upsert error: {e}")
        return None

    def delete_user(self, user_id: int) -> bool:
        """Delete a user row from Supabase."""
        if not self.is_configured or not user_id:
            return False
        url = f"{self.base_url}/rest/v1/users?id=eq.{user_id}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.delete(url, headers=self._headers(prefer_return=False))
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Supabase delete user error: {e}")
            return False

    def sync_applications_to_cloud(self, session: Any) -> int:
        """Push all local applications to Supabase."""
        if not self.is_configured:
            return 0
        from app.models import Application
        apps = session.query(Application).all()
        pushed = 0
        for a in apps:
            data = {
                "id": a.id,
                "job_id": a.job_id,
                "email": a.email or "portal-application@careerpulse.internal",
                "job_title": a.job_title or "",
                "company": a.company or "",
                "link": a.link or "",
                "jd_text": (a.jd_text or "")[:4000],
                "drafted_email": (a.drafted_email or "")[:4000],
                "subject": a.subject or "",
                "resume_path": a.resume_path or "",
                "template_id": a.template_id or "apex_modern",
                "ats_score": float(a.ats_score or 0.0),
                "ats_attempts": int(a.ats_attempts or 0),
                "status": a.status or "pending",
                "disposition": a.disposition or "",
                "sent_at": a.sent_at.isoformat() if a.sent_at else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
            res = self.upsert_application(data, session=session)
            if res:
                pushed += 1
        return pushed

    def fetch_all_resume_versions(self, application_id: int | None = None) -> list[dict[str, Any]]:
        """Fetch stored resume versions from Supabase public.resume_versions table."""
        if not self.is_configured:
            return []
        query_params = ["select=*"]
        if application_id is not None:
            query_params.append(f"application_id=eq.{application_id}")
        query_params.append("order=version_no.desc")
        url = f"{self.base_url}/rest/v1/resume_versions?{'&'.join(query_params)}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
                elif res.status_code == 404 or "does not exist" in res.text:
                    logger.debug("Supabase resume_versions table does not exist remotely yet.")
                    return []
        except Exception as e:
            logger.warning(f"Supabase fetch_all_resume_versions error: {e}")
        return []

    def upsert_resume_version(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Upsert a resume version row into Supabase public.resume_versions table."""
        if not self.is_configured:
            return None
        clean_data = {k: v for k, v in data.items() if v is not None}
        app_id = clean_data.get("application_id")
        ver_no = clean_data.get("version_no")
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                if app_id is not None and ver_no is not None:
                    patch_url = f"{self.base_url}/rest/v1/resume_versions?application_id=eq.{app_id}&version_no=eq.{ver_no}"
                    patch_res = client.patch(patch_url, json=clean_data, headers=self._headers(prefer_return=True))
                    if patch_res.status_code == 200 and patch_res.json():
                        return patch_res.json()[0]

                post_url = f"{self.base_url}/rest/v1/resume_versions"
                headers = self._headers(prefer_return=True)
                headers["Prefer"] = "resolution=merge-duplicates,return=representation"
                post_res = client.post(post_url, json=clean_data, headers=headers)
                if post_res.status_code in (200, 201):
                    rows = post_res.json()
                    return rows[0] if isinstance(rows, list) and rows else rows
                logger.debug(f"Supabase resume_version upsert status {post_res.status_code}: {post_res.text[:120]}")
        except Exception as e:
            logger.warning(f"Supabase resume_version upsert error: {e}")
        return None

    def delete_resume_version(self, application_id: int, version_no: int) -> bool:
        """Delete a resume version row from Supabase."""
        if not self.is_configured or not application_id or not version_no:
            return False
        url = f"{self.base_url}/rest/v1/resume_versions?application_id=eq.{application_id}&version_no=eq.{version_no}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.delete(url, headers=self._headers(prefer_return=False))
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Supabase delete resume_version error: {e}")
            return False

    def insert_email_activity(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Insert an email activity event row into Supabase public.email_activities table."""
        if not self.is_configured:
            return None
        clean_data = {k: v for k, v in data.items() if v is not None}
        url = f"{self.base_url}/rest/v1/email_activities"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.post(url, json=clean_data, headers=self._headers(prefer_return=True))
                if res.status_code in (200, 201):
                    rows = res.json()
                    return rows[0] if isinstance(rows, list) and rows else rows
                logger.debug(f"Supabase email_activity insert returned {res.status_code}: {res.text[:120]}")
        except Exception as e:
            logger.debug(f"Supabase email_activity insert error: {e}")
        return None

    def fetch_all_email_activities(self, user_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch stored email activities from Supabase public.email_activities table."""
        if not self.is_configured:
            return []
        query_params = ["select=*"]
        if user_id is not None:
            query_params.append(f"user_id=eq.{user_id}")
        query_params.append(f"order=created_at.desc&limit={limit}")
        url = f"{self.base_url}/rest/v1/email_activities?{'&'.join(query_params)}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Supabase fetch_all_email_activities error: {e}")
        return []

    def update_job_status(
        self,
        job_id: int | None = None,
        status: str = "active",
        dedup_key: str | None = None,
        session: Any = None,
    ) -> bool:
        """Update job status in Supabase (e.g. active -> applied, expired).
        
        Safely resolves local job_id to Supabase record using dedup_key.
        """
        if not self.is_configured:
            return False

        target_key = dedup_key
        if not target_key and job_id and session:
            try:
                from app.models import Job
                j = session.get(Job, job_id)
                if j and j.dedup_key:
                    target_key = j.dedup_key
            except Exception:
                pass

        if target_key:
            url = f"{self.base_url}/rest/v1/jobs?dedup_key=eq.{target_key}"
        elif job_id:
            url = f"{self.base_url}/rest/v1/jobs?id=eq.{job_id}"
        else:
            return False

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.patch(url, json={"status": status}, headers=self._headers(prefer_return=False))
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Supabase status update skipped: {e}")
            return False

    def delete_job(
        self,
        job_id: int | None = None,
        dedup_key: str | None = None,
        session: Any = None,
    ) -> bool:
        """Delete a job row from Supabase public.jobs table."""
        if not self.is_configured:
            return False

        target_key = dedup_key
        if not target_key and job_id and session:
            try:
                from app.models import Job
                j = session.get(Job, job_id)
                if j and j.dedup_key:
                    target_key = j.dedup_key
            except Exception:
                pass

        if target_key:
            url = f"{self.base_url}/rest/v1/jobs?dedup_key=eq.{target_key}"
        elif job_id:
            url = f"{self.base_url}/rest/v1/jobs?id=eq.{job_id}"
        else:
            return False

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.delete(url, headers=self._headers(prefer_return=False))
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Supabase delete job error: {e}")
            return False

    def delete_application(self, app_id: int) -> bool:
        """Delete an application row from Supabase public.applications table."""
        if not self.is_configured or not app_id:
            return False
        url = f"{self.base_url}/rest/v1/applications?id=eq.{app_id}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.delete(url, headers=self._headers(prefer_return=False))
                return res.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"Supabase delete application error: {e}")
            return False

    def fetch_all_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch all job records from Supabase public.jobs table."""
        if not self.is_configured:
            return []
        url = f"{self.base_url}/rest/v1/jobs?select=*&order=created_at.desc&limit={limit}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.get(url, headers=self._headers(prefer_return=False))
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
                logger.warning(f"Supabase fetch_all_jobs returned {res.status_code}: {res.text[:150]}")
        except Exception as e:
            logger.warning(f"Supabase fetch_all_jobs error: {e}")
        return []

    def sync_cloud_to_local(self, session: Any) -> int:
        """Pull jobs from Supabase cloud into local database, deduplicating by dedup_key."""
        from app.models import Job
        import datetime as dt

        def _parse_iso(val: Any) -> dt.datetime | None:
            if not val:
                return None
            try:
                return dt.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except Exception:
                return None

        cloud_jobs = self.fetch_all_jobs(limit=1000)
        if not cloud_jobs:
            return 0

        existing_keys = {k[0] for k in session.query(Job.dedup_key).all()}
        new_count = 0

        for r in cloud_jobs:
            k = r.get("dedup_key")
            if not k or k in existing_keys:
                continue

            job = Job(
                dedup_key=k,
                title=r.get("title") or "",
                company=r.get("company") or "",
                location=r.get("location") or "",
                link=r.get("link") or "",
                has_email=bool(r.get("has_email")),
                email=r.get("email"),
                jd_text=r.get("jd_text") or "",
                deadline=_parse_iso(r.get("deadline")),
                status=r.get("status") or "active",
                source=r.get("source") or "",
                created_at=_parse_iso(r.get("created_at")) or dt.datetime.now(dt.timezone.utc),
                expires_at=_parse_iso(r.get("expires_at")),
            )
            session.add(job)
            existing_keys.add(k)
            new_count += 1

        if new_count > 0:
            session.commit()
            logger.info(f"Synced {new_count} jobs from Supabase Cloud to local database.")

        return new_count

