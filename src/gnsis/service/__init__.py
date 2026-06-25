"""GNSIS service layer — the Railway deployment surface.

Two processes share this package:

* ``web`` — the FastAPI app (:mod:`gnsis.service.api`): create jobs, read status /
  logs / diff, approve or reject.
* ``worker`` — the Celery app (:mod:`gnsis.service.tasks`): run the long
  generation pipeline and, after approval, open the PR.

Everything durable lives in Postgres (:mod:`gnsis.service.orm`); the queue is
Redis. These modules require the ``service`` optional dependencies; the GNSIS
core does not import them.
"""
