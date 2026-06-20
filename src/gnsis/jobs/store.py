"""Job persistence.

``JobStore`` is the interface the API and worker share; ``FileJobStore`` is the
local/offline implementation. A Postgres-backed store (Railway) will implement
the same four methods so nothing above this line changes.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional

from .models import Job


class JobStore(ABC):
    @abstractmethod
    def create(self, job: Job) -> Job: ...

    @abstractmethod
    def get(self, job_id: str) -> Optional[Job]: ...

    @abstractmethod
    def save(self, job: Job) -> Job: ...

    @abstractmethod
    def list(self) -> List[Job]: ...


class FileJobStore(JobStore):
    def __init__(self, workdir: str) -> None:
        self.root = os.path.join(workdir, "jobs")

    def _path(self, job_id: str) -> str:
        return os.path.join(self.root, f"{job_id}.json")

    def _write(self, job: Job) -> Job:
        os.makedirs(self.root, exist_ok=True)
        path = self._path(job.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(job.to_dict(), handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return job

    def create(self, job: Job) -> Job:
        if os.path.exists(self._path(job.id)):
            raise ValueError(f"job {job.id} already exists")
        return self._write(job)

    def get(self, job_id: str) -> Optional[Job]:
        path = self._path(job_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return Job.from_dict(json.load(handle))

    def save(self, job: Job) -> Job:
        job.touch()
        return self._write(job)

    def list(self) -> List[Job]:
        if not os.path.isdir(self.root):
            return []
        jobs: List[Job] = []
        for fname in os.listdir(self.root):
            if fname.endswith(".json"):
                job = self.get(fname[:-5])
                if job is not None:
                    jobs.append(job)
        return sorted(jobs, key=lambda j: j.created_at)
