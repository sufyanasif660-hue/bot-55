#!/bin/sh
set -e
exec gunicorn --bind "0.0.0.0:${PORT:-5000}" --workers 1 --threads 4 webapp:app
