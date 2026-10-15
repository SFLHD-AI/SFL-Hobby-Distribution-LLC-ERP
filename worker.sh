#!/bin/sh
# Background worker (Django-Q): bank sync, recurring expenses, digests, tracking.
exec python manage.py qcluster
