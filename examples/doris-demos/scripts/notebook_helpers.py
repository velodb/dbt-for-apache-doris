import csv
import html
import os
import re
import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path

from IPython.display import HTML, display


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DBT_SUMMARY = re.compile(
    r"Done\. PASS=(\d+) WARN=(\d+) ERROR=(\d+) SKIP=(\d+) .*? TOTAL=(\d+)"
)

STYLES = """
<style>
.doris-cover {
  max-width: 960px;
  border-top: 4px solid #0f766e;
  padding: 22px 0 18px;
  margin: 0 0 24px;
}
.doris-cover-kicker {
  color: #0f766e;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.doris-cover-title {
  color: #17212b;
  font-size: 30px;
  line-height: 1.2;
  font-weight: 750;
  margin: 0 0 8px;
}
.doris-cover-lead {
  color: #475569;
  font-size: 15px;
  line-height: 1.7;
  max-width: 780px;
  margin: 0;
}
.doris-cover-note {
  display: inline-block;
  border: 1px solid #99f6e4;
  border-radius: 4px;
  background: #f0fdfa;
  color: #115e59;
  padding: 6px 10px;
  margin-top: 14px;
  font-size: 12px;
}
.doris-index {
  width: 100%;
  max-width: 960px;
  border-collapse: collapse;
  margin: 14px 0 6px;
  font-size: 13px;
}
.doris-index th {
  background: #f1f5f9;
  border-bottom: 2px solid #cbd5e1;
  color: #334155;
  padding: 9px 10px;
  text-align: left;
}
.doris-index td {
  border-bottom: 1px solid #e2e8f0;
  color: #475569;
  padding: 10px;
  vertical-align: top;
}
.doris-index tbody tr:hover { background: #f8fafc; }
.doris-index-number {
  color: #0f766e;
  font-size: 16px;
  font-weight: 750;
  width: 44px;
}
.doris-index-name { color: #17212b; font-weight: 700; }
.doris-flow {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 8px;
  max-width: 960px;
  margin: 16px 0 22px;
}
.doris-flow-step {
  flex: 1 1 145px;
  border: 1px solid #dbe4e8;
  border-left: 3px solid #0f766e;
  border-radius: 4px;
  background: #f8fbfb;
  color: #334155;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.45;
}
.doris-flow-step strong { color: #17212b; display: block; margin-bottom: 2px; }
.doris-flow-arrow {
  align-self: center;
  color: #94a3b8;
  font-size: 18px;
  line-height: 1;
}
.doris-status {
  border: 1px solid #d9dee5;
  border-left: 4px solid #6b7280;
  border-radius: 4px;
  background: #f8fafc;
  padding: 14px 16px;
  margin: 8px 0 12px;
  color: #18212f;
}
.doris-status.running { border-left-color: #d97706; background: #fffbeb; }
.doris-status.success { border-left-color: #16803c; background: #f0fdf4; }
.doris-status.failure { border-left-color: #c62828; background: #fff5f5; }
.doris-status-title { font-size: 16px; font-weight: 650; margin-bottom: 7px; }
.doris-meta { display: flex; flex-wrap: wrap; gap: 7px; }
.doris-chip {
  display: inline-block;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  background: #ffffff;
  padding: 3px 8px;
  font-size: 12px;
  color: #334155;
}
.doris-result { margin: 10px 0 18px; }
.doris-result-final {
  border-left: 3px solid #0f766e;
  background: #f0fdfa;
  padding: 10px 12px 2px;
  max-width: 920px;
}
.doris-result-title { font-size: 14px; font-weight: 650; margin: 0 0 6px; color: #18212f; }
.doris-result-count { color: #64748b; font-size: 12px; font-weight: 400; margin-left: 6px; }
.doris-result-table-wrap { overflow-x: auto; }
table.doris-table { border-collapse: collapse; width: auto; min-width: 420px; font-size: 13px; }
table.doris-table th {
  background: #eef2f6;
  border: 1px solid #cbd5e1;
  color: #1f2937;
  padding: 7px 10px;
  text-align: left;
}
table.doris-table td { border: 1px solid #d9dee5; padding: 7px 10px; }
table.doris-table tbody tr:nth-child(even) { background: #f8fafc; }
.doris-code { margin: 8px 0 16px; max-width: 920px; }
.doris-code-title { font-size: 14px; font-weight: 650; margin: 0 0 6px; color: #18212f; }
.doris-code pre {
  max-height: 440px;
  overflow: auto;
  white-space: pre;
  border: 1px solid #d9dee5;
  border-radius: 4px;
  background: #f8fafc;
  color: #18212f;
  padding: 12px;
  font-size: 12px;
  line-height: 1.5;
}
details.doris-log { margin: 4px 0 16px; color: #475569; }
details.doris-log summary { cursor: pointer; font-size: 12px; user-select: none; }
details.doris-log pre {
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  border: 1px solid #d9dee5;
  border-radius: 4px;
  background: #111827;
  color: #e5e7eb;
  padding: 12px;
  font-size: 11px;
  line-height: 1.45;
}
@media (max-width: 700px) {
  .doris-cover-title { font-size: 24px; }
  .doris-flow-arrow { display: none; }
  .doris-flow-step { flex-basis: 100%; }
  table.doris-index { font-size: 12px; }
  .doris-index th:nth-child(3), .doris-index td:nth-child(3) { display: none; }
}
</style>
"""


def find_repo_root(start):
    for candidate in (start, *start.parents):
        if (candidate / "examples").is_dir():
            return candidate
    raise FileNotFoundError("Start Jupyter from the dbt-for-apache-doris repository or a subdirectory.")


def clean_log(output):
    return ANSI_ESCAPE.sub("", output).strip()


class DemoRunner:
    def __init__(self, start=None):
        self.repo_root = find_repo_root((start or Path.cwd()).resolve())
        self.examples_root = self.repo_root / "examples"
        self.dbt_bin = os.environ.get("DBT_BIN") or shutil.which("dbt")
        self.mysql_bin = os.environ.get("MYSQL_BIN") or shutil.which("mysql")
        if not self.dbt_bin:
            raise RuntimeError("dbt was not found. Run the environment setup first or set DBT_BIN.")
        if not self.mysql_bin:
            raise RuntimeError("mysql was not found. Install a MySQL client or set MYSQL_BIN.")

        self.env = os.environ.copy()
        self.env.update(
            {
                "DBT_BIN": self.dbt_bin,
                "MYSQL_BIN": self.mysql_bin,
                "DORIS_HOST": os.environ.get("DORIS_HOST", "127.0.0.1"),
                "DORIS_PORT": os.environ.get("DORIS_PORT", "9030"),
                "DORIS_USER": os.environ.get("DORIS_USER", "root"),
                "DORIS_PASSWORD": os.environ.get("DORIS_PASSWORD", ""),
            }
        )
        if self.env["DORIS_PASSWORD"]:
            self.env["MYSQL_PWD"] = self.env["DORIS_PASSWORD"]
        else:
            self.env.pop("MYSQL_PWD", None)
        display(HTML(STYLES))

    def _run(self, command, cwd=None):
        return subprocess.run(
            command,
            cwd=cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    def _mysql_command(self, sql):
        return [
            self.mysql_bin,
            "--batch",
            "--raw",
            "-h",
            self.env["DORIS_HOST"],
            "-P",
            self.env["DORIS_PORT"],
            "-u",
            self.env["DORIS_USER"],
            "-e",
            sql,
        ]

    @staticmethod
    def _status(kind, title, chips):
        chip_html = "".join(
            f'<span class="doris-chip">{html.escape(str(chip))}</span>' for chip in chips
        )
        return HTML(
            f'<div class="doris-status {kind}">'
            f'<div class="doris-status-title">{html.escape(title)}</div>'
            f'<div class="doris-meta">{chip_html}</div></div>'
        )

    @staticmethod
    def _log_details(output, opened=False):
        open_attribute = " open" if opened else ""
        return HTML(
            f'<details class="doris-log"{open_attribute}>'
            '<summary>View full run log</summary>'
            f'<pre>{html.escape(clean_log(output))}</pre></details>'
        )

    @staticmethod
    def show_file(title, path):
        path = Path(path)
        display(
            HTML(
                '<div class="doris-code">'
                f'<div class="doris-code-title">{html.escape(title)} '
                f'<span class="doris-result-count">{html.escape(path.name)}</span></div>'
                f'<pre>{html.escape(path.read_text())}</pre></div>'
            )
        )

    @staticmethod
    def show_sql(title, sql):
        display(
            HTML(
                '<div class="doris-code">'
                f'<div class="doris-code-title">{html.escape(title)}</div>'
                f'<pre>{html.escape(sql.strip())}</pre></div>'
            )
        )

    def _run_step(self, title, command, cwd=None):
        handle = display(
            self._status("running", f"Running: {title}", ["Command in progress"]),
            display_id=True,
        )
        started = time.perf_counter()
        result = self._run(command, cwd=cwd)
        elapsed = time.perf_counter() - started
        output = clean_log(result.stdout)
        summaries = DBT_SUMMARY.findall(output)

        if result.returncode != 0:
            handle.update(
                self._status(
                    "failure",
                    f"Failed: {title}",
                    [f"{elapsed:.1f} s", f"exit {result.returncode}"],
                )
            )
            if output:
                display(self._log_details(result.stdout, opened=True))
            raise RuntimeError(f"{title} failed.")

        chips = [f"{elapsed:.1f} s"]
        if summaries:
            passed = sum(int(summary[0]) for summary in summaries)
            chips.extend([f"{passed} successful nodes", "dbt passed"])
        else:
            chips.append("Succeeded")
        handle.update(self._status("success", f"Passed: {title}", chips))
        if output:
            display(self._log_details(result.stdout))

    def run_sql_file(self, title, path):
        path = Path(path)
        self._run_step(title, self._mysql_command(path.read_text()), cwd=path.parent)

    def run_sql(self, title, sql):
        self._run_step(title, self._mysql_command(sql))

    def run_dbt(self, title, project_dir, *arguments):
        project_dir = Path(project_dir)
        command = [
            self.dbt_bin,
            *arguments,
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(project_dir),
        ]
        self._run_step(title, command, cwd=project_dir)

    def run_script(self, title, path):
        path = Path(path)
        self._run_step(title, [str(path)], cwd=path.parent.parent)

    def show_environment(self):
        version_result = self._run([self.dbt_bin, "--version"])
        if version_result.returncode != 0:
            display(
                self._status(
                    "failure",
                    "Environment check failed",
                    ["dbt --version", f"exit {version_result.returncode}"],
                )
            )
            display(self._log_details(version_result.stdout, opened=True))
            raise RuntimeError("dbt environment check failed.")

        installed = re.search(r"installed:\s*([^\s]+)", clean_log(version_result.stdout))
        dbt_version = installed.group(1) if installed else "available"

        daily_project = self.examples_root / "doris-daily-order-summary"
        debug_result = self._run(
            [
                self.dbt_bin,
                "debug",
                "--project-dir",
                str(daily_project),
                "--profiles-dir",
                str(daily_project),
            ],
            cwd=daily_project,
        )
        if debug_result.returncode != 0:
            display(
                self._status(
                    "failure",
                    "Environment check failed",
                    ["dbt debug", f"exit {debug_result.returncode}"],
                )
            )
            display(self._log_details(debug_result.stdout, opened=True))
            raise RuntimeError("The dbt Doris adapter or connection check failed.")

        adapter_match = re.search(
            r"(?:adapter version:\s*|Registered adapter:\s*doris=)([^\s]+)",
            clean_log(debug_result.stdout),
        )
        adapter_version = adapter_match.group(1) if adapter_match else "loaded"

        backend_rows = self.query(
            "Doris Backend",
            "show backends",
            columns=["Host", "Alive", "Version", "TabletNum"],
        )
        if not any(row[1].lower() == "true" for row in backend_rows):
            display(self._status("failure", "Environment check failed", ["No Alive=true Backend"]))
            raise RuntimeError("The Doris Backend is not ready.")

        query_rows = self.query(
            "Doris BE query",
            'select sum(number) as result from numbers("number"="10")',
            columns=["result"],
        )
        if query_rows != [["45"]]:
            display(self._status("failure", "Environment check failed", ["The BE query returned an unexpected result"]))
            raise RuntimeError("The Doris Backend query check failed.")

        display(
            self._status(
                "success",
                "Execution environment ready",
                [
                    f"dbt Core {dbt_version}",
                    f"Doris adapter {adapter_version}",
                    f"Doris {self.env['DORIS_HOST']}:{self.env['DORIS_PORT']}",
                    f"Repository {self.repo_root.name}",
                ],
            )
        )

    def query(self, title, sql, columns=None):
        result = self._run(self._mysql_command(sql))
        if result.returncode != 0:
            display(self._status("failure", f"Query failed: {title}", [f"exit {result.returncode}"]))
            display(self._log_details(result.stdout, opened=True))
            raise RuntimeError(f"Doris query failed: {title}")

        rows = list(csv.reader(StringIO(result.stdout), delimiter="\t"))
        if not rows:
            display(self._status("success", title, ["0 rows"]))
            return []

        headers = rows[0]
        data = rows[1:]
        if columns:
            missing = [column for column in columns if column not in headers]
            if missing:
                raise ValueError(f"Query result is missing columns: {', '.join(missing)}")
            indexes = [headers.index(column) for column in columns]
            headers = columns
            data = [[row[index] for index in indexes] for row in data]

        header_html = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
        body_html = "".join(
            "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
            for row in data
        )
        final_markers = ("Final result", "Output:", "After merge", "After refresh", "Second Snapshot", "Idempotent")
        result_class = " doris-result-final" if title.startswith(final_markers) else ""
        display(
            HTML(
                f'<div class="doris-result{result_class}">'
                f'<div class="doris-result-title">{html.escape(title)}'
                f'<span class="doris-result-count">{len(data)} rows</span></div>'
                '<div class="doris-result-table-wrap">'
                f'<table class="doris-table"><thead><tr>{header_html}</tr></thead>'
                f'<tbody>{body_html}</tbody></table></div></div>'
            )
        )
        return data
