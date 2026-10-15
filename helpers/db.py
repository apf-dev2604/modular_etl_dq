import os
import socket
from typing import Optional

import psycopg2
from psycopg2.extras import register_uuid

from .validators import validate_db_env


def probe_host_port(host: str, port: int, timeout_sec: int = 5) -> None:
    """TCP reachability check. Silent by default to keep ETL output clean.

    Set ETL_VERBOSE_PROBE=1 to print probe status during troubleshooting.
    """
    verbose = str(os.getenv("ETL_VERBOSE_PROBE") or "").strip().lower() in ("1", "true", "yes", "y", "on")
    if verbose:
        print(f"Probing TCP {host}:{port} ...", flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_sec)
    try:
        sock.connect((host, port))
        if verbose:
            print("TCP reachable", flush=True)
    except Exception as exc:
        if verbose:
            print(f"TCP probe failed: {exc}", flush=True)
        raise
    finally:
        try:
            sock.close()
        except Exception:
            pass


def env_name(prefix: Optional[str], key: str) -> str:
    return f"{prefix}_{key}" if prefix else key


def connect(dbname: str, env_prefix: Optional[str] = None):
    """Create a PostgreSQL connection using environment-based secrets.

    Passwords are never stored in config.py and are never printed. For separate
    source/target credentials, set SOURCE_RDS_* and TARGET_RDS_* values. If a
    prefixed value is absent, the generic RDS_* value is used.
    """

    validate_db_env(env_prefix)

    def get(name: str, default: str = "") -> str:
        prefixed = os.getenv(env_name(env_prefix, name)) if env_prefix else None
        return prefixed if prefixed is not None else os.getenv(name, default)

    # Host has no secret value, but it is still required by validate_db_env.
    host = get("RDS_HOST")
    user = get("RDS_USER")
    password = get("RDS_PASSWORD")
    port = int(get("RDS_PORT", "5432"))
    sslmode = get("RDS_SSLMODE", "require")

    probe_host_port(host, port, timeout_sec=5)
    conn = psycopg2.connect(
        host=host,
        user=user,
        password=password,
        port=port,
        dbname=dbname,
        connect_timeout=45,
        sslmode=sslmode,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    register_uuid(conn)
    conn.autocommit = False
    return conn


def quote_ident(name: str) -> str:
    text = str(name)
    # Prefer unquoted PostgreSQL identifiers when safe. This supports the new
    # snake_case target tables/columns while still protecting existing source
    # tables with mixed-case names such as GameTransactionInplayV1.
    if text.replace("_", "").isalnum() and text == text.lower() and not text[0].isdigit():
        return text
    return '"' + text.replace('"', '""') + '"'


# Target fact tables are intentionally recreated as unquoted PostgreSQL identifiers.
# Important PostgreSQL rule: unquoted mixed-case tokens are stored and resolved as lowercase.
# Example SQL token gameTransaction_prod is physically gametransaction_prod.
# Source datalake tables such as "GameTransactionInplayV1" remain quoted.
UNQUOTED_TARGET_TABLES = {
    "gametransaction_prod",
    "wallettransaction_prod",
    "gameTransaction_prod",
    "walletTransaction_prod",
}

def table_ref(schema: Optional[str], table: str) -> str:
    if table in UNQUOTED_TARGET_TABLES or table.lower() in UNQUOTED_TARGET_TABLES:
        tbl = table
        if schema:
            return f"{quote_ident(schema)}.{tbl}"
        return tbl
    if schema:
        return f"{quote_ident(schema)}.{quote_ident(table)}"
    return quote_ident(table)
