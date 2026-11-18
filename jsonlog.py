"""
JSON logs for both portals: one line per event, with the level name, the module, the message and,
when a request is about one application, its employee ID, and for the HR portal the HR user's login name (hr_user).

Nothing else about a person is logged: messages are written by our code and carry IDs and counts,
and an exception is logged as its type and the place it was raised, never its text (an exception
text can quote what was being parsed, such as a name or a document number).

    LOG_LEVEL   INFO by default; DEBUG only when set, and never on AWS unless someone sets it
    LOG_FORMAT  json (default) or text, for reading logs on a local machine

CloudWatch metric filters read the "levelname" field, e.g. { $.levelname = "ERROR" }.
"""

import contextvars
import json
import logging
import os
import sys
import traceback

employee_id_var = contextvars.ContextVar("employee_id", default=None)
hr_user_var = contextvars.ContextVar("hr_user", default=None)


def set_hr_user(user):
    """Tag the log lines of the current request with the HR user's login name (not a name or an e-mail)."""

    hr_user_var.set(user)


def set_employee(employee_id):
    """Tag the log lines of the current request with this employee ID (and nothing else about them)."""

    employee_id_var.set(employee_id)


class JsonFormatter(logging.Formatter):

    def format(self, record):
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "levelname": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        employee_id = getattr(record, "employee_id", None) or employee_id_var.get()
        if employee_id:
            entry["employee_id"] = employee_id

        hr_user = getattr(record, "hr_user", None) or hr_user_var.get()
        if hr_user:
            entry["hr_user"] = hr_user

        if record.exc_info and record.exc_info[0]:
            entry["error_type"] = record.exc_info[0].__name__
            frames = traceback.extract_tb(record.exc_info[2])
            if frames:
                last = frames[-1]
                entry["where"] = f"{os.path.basename(last.filename)}:{last.lineno} {last.name}"

        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Same rule as the JSON one: the type and place of an exception, not its text."""

    def format(self, record):
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname} {record.name}: {record.getMessage()}"

        employee_id = getattr(record, "employee_id", None) or employee_id_var.get()
        if employee_id:
            line += f" employee_id={employee_id}"

        hr_user = getattr(record, "hr_user", None) or hr_user_var.get()
        if hr_user:
            line += f" hr_user={hr_user}"

        if record.exc_info and record.exc_info[0]:
            line += f" error_type={record.exc_info[0].__name__}"

        return line


def setup():
    """Send every log line to stdout (CloudWatch Logs on Lambda) in the chosen format."""

    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    text = os.environ.get("LOG_FORMAT", "json").strip().lower() == "text"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if text else JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Library debug output can hold request details; keep it quiet whatever LOG_LEVEL says
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer", "multipart", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
