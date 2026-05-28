"""
app/connectors/source_db.py — crawl_source_db
"""
from __future__ import annotations

import re
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, unquote

from app.config import _DB_INSTALL_HINTS, _INFOSYS_QUERY
from app.parsers.schema import _normalize_type


# ── URL parsers for native-driver paths (Oracle / MSSQL / Snowflake) ─────────
def _parse_oracle_url(conn_str: str) -> Optional[dict]:
    """Parse `oracle://user:pass@host:port/service` → kwargs dict for oracledb.connect.
    Returns None if conn_str is not a recognizable plain oracle URL (e.g. the
    SQLAlchemy form `oracle+cx_oracle://...` is left for the legacy path)."""
    if not isinstance(conn_str, str) or "://" not in conn_str:
        return None
    try:
        p = urlparse(conn_str)
    except Exception:
        return None
    if p.scheme.lower() != "oracle":
        return None
    user = unquote(p.username) if p.username else ""
    password = unquote(p.password) if p.password else ""
    host = p.hostname or ""
    port = p.port or 1521
    service = (p.path or "").lstrip("/").split("?")[0] or ""
    qs = parse_qs(p.query or "")
    if not service and "service_name" in qs:
        service = qs["service_name"][0]
    if not (host and service):
        return None
    return {"user": user, "password": password, "dsn": f"{host}:{port}/{service}"}


def _parse_mssql_url(conn_str: str) -> Optional[str]:
    """Parse `mssql://user:pass@host[:port]/db?driver=...` → pyodbc connection string.
    Returns None if not a recognizable plain mssql URL (SQLAlchemy form
    `mssql+pyodbc://...` is left for the legacy path)."""
    if not isinstance(conn_str, str) or "://" not in conn_str:
        return None
    try:
        p = urlparse(conn_str)
    except Exception:
        return None
    scheme = p.scheme.lower()
    if scheme not in ("mssql", "azuresql", "sqlserver"):
        return None
    user = unquote(p.username) if p.username else ""
    password = unquote(p.password) if p.password else ""
    host = p.hostname or ""
    port = p.port
    db = (p.path or "").lstrip("/").split("?")[0] or ""
    qs = parse_qs(p.query or "")
    driver = qs.get("driver", ["ODBC Driver 17 for SQL Server"])[0].replace("+", " ")
    trusted = qs.get("trusted_connection", ["no"])[0].lower() in ("yes", "true", "1")
    server = f"{host},{port}" if port else host
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}"]
    if db:
        parts.append(f"DATABASE={db}")
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        if user:
            parts.append(f"UID={user}")
        if password:
            parts.append(f"PWD={password}")
    return ";".join(parts) + ";"


def _parse_snowflake_url(conn_str: str) -> Optional[dict]:
    """Parse `snowflake://user:pass@account/database/schema?warehouse=WH` → kwargs.
    Returns None if not a recognizable plain snowflake URL (the SQLAlchemy form
    is identical, so we additionally require the snowflake-connector library
    to be importable before claiming this path)."""
    if not isinstance(conn_str, str) or "://" not in conn_str:
        return None
    try:
        p = urlparse(conn_str)
    except Exception:
        return None
    if p.scheme.lower() != "snowflake":
        return None
    user = unquote(p.username) if p.username else ""
    password = unquote(p.password) if p.password else ""
    account = p.hostname or ""
    path_parts = [seg for seg in (p.path or "").lstrip("/").split("/") if seg]
    database = path_parts[0] if len(path_parts) >= 1 else ""
    sf_schema = path_parts[1] if len(path_parts) >= 2 else ""
    qs = parse_qs(p.query or "")
    warehouse = qs.get("warehouse", [""])[0]
    role = qs.get("role", [""])[0]
    if not account:
        return None
    out: dict = {"account": account, "user": user, "password": password}
    if warehouse:
        out["warehouse"] = warehouse
    if database:
        out["database"] = database
    if sf_schema:
        out["schema"] = sf_schema
    if role:
        out["role"] = role
    return out


def _apply_filters(tables: Dict, schema_filter: str, table_filter: str) -> Dict:
    """Apply schema_filter and table_filter (same logic as the legacy tail)."""
    if table_filter:
        allowed = {t.strip().lower() for t in table_filter.split(",") if t.strip()}
        tables = {k: v for k, v in tables.items() if k.lower() in allowed}
    if schema_filter:
        schemas = {s.strip().lower() for s in schema_filter.split(",") if s.strip()}
        filtered = {}
        for tbl, cols in tables.items():
            parts = tbl.split(".")
            if len(parts) == 2 and parts[0].lower() not in schemas:
                continue
            filtered[tbl] = cols
        if filtered:
            tables = filtered
    return tables


def crawl_source_db(db_type, conn_str, schema_filter: str = "", table_filter: str = "") -> Dict:
    """Crawl a source database and return schema in parse_schema_file format."""
    db_type = str(db_type).lower()

    # ── Native-driver fast paths (Oracle / MSSQL / Snowflake) ────────────────
    # Try the native driver first when conn_str looks parseable (URL or kwargs
    # dict). On ImportError → raise ValueError with install instructions. On
    # parse miss → fall through to the existing sqlalchemy-based branches.
    if db_type == "oracle":
        kwargs = conn_str if isinstance(conn_str, dict) else _parse_oracle_url(conn_str)
        if kwargs:
            try:
                import oracledb  # type: ignore
            except ImportError:
                raise ValueError(
                    "Oracle driver not installed. Install oracledb: pip install oracledb"
                )
            try:
                _ora_conn = oracledb.connect(
                    user=kwargs.get("user", ""),
                    password=kwargs.get("password", ""),
                    dsn=kwargs.get("dsn", ""),
                )
            except Exception as e:
                raise RuntimeError(f"Oracle connection failed: {e}")
            try:
                _cur = _ora_conn.cursor()
                owner = (schema_filter or kwargs.get("schema") or "").split(",")[0].strip()
                if owner:
                    _cur.execute(
                        "SELECT table_name, column_name, data_type, nullable "
                        "FROM all_tab_columns WHERE owner = UPPER(:schema) "
                        "ORDER BY table_name, column_id",
                        schema=owner,
                    )
                else:
                    _cur.execute(
                        "SELECT table_name, column_name, data_type, nullable "
                        "FROM user_tab_columns ORDER BY table_name, column_id"
                    )
                _rows = _cur.fetchall()
                _cur.close()
            finally:
                try:
                    _ora_conn.close()
                except Exception:
                    pass
            _t: Dict = {}
            for r in _rows:
                _tbl = str(r[0])
                _t.setdefault(_tbl, []).append({
                    "name": str(r[1]),
                    "type": _normalize_type(str(r[2])),
                    "sample": "",
                    "nullable": str(r[3]).upper() == "Y",
                })
            _t = _apply_filters(_t, schema_filter, table_filter)
            return {"tables": [{"name": k, "columns": v} for k, v in _t.items()]}

    if db_type == "mssql":
        if isinstance(conn_str, dict):
            _parts = [f"{k}={v}" for k, v in conn_str.items() if v not in (None, "")]
            _pyodbc_dsn = ";".join(_parts) + ";" if _parts else None
            _schema_for_q = conn_str.get("schema", schema_filter) or "dbo"
        else:
            _pyodbc_dsn = _parse_mssql_url(conn_str)
            _schema_for_q = (schema_filter or "dbo").split(",")[0].strip() or "dbo"
        if _pyodbc_dsn:
            try:
                import pyodbc  # type: ignore
            except ImportError:
                raise ValueError(
                    "MSSQL driver not installed. Install pyodbc: pip install pyodbc"
                )
            try:
                _ms_conn = pyodbc.connect(_pyodbc_dsn)
            except Exception as e:
                raise RuntimeError(f"MSSQL connection failed: {e}")
            try:
                _cur = _ms_conn.cursor()
                _cur.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    _schema_for_q,
                )
                _rows = _cur.fetchall()
                _cur.close()
            finally:
                try:
                    _ms_conn.close()
                except Exception:
                    pass
            _t = {}
            for r in _rows:
                _tbl = str(r[0])
                _t.setdefault(_tbl, []).append({
                    "name": str(r[1]),
                    "type": _normalize_type(str(r[2])),
                    "sample": "",
                    "nullable": str(r[3]).upper() == "YES",
                })
            _t = _apply_filters(_t, schema_filter, table_filter)
            return {"tables": [{"name": k, "columns": v} for k, v in _t.items()]}

    if db_type == "snowflake":
        kwargs = conn_str if isinstance(conn_str, dict) else _parse_snowflake_url(conn_str)
        if kwargs:
            try:
                import snowflake.connector  # type: ignore
            except ImportError:
                raise ValueError(
                    "Snowflake driver not installed. "
                    "Install snowflake-connector-python: pip install snowflake-connector-python"
                )
            try:
                _sf_conn = snowflake.connector.connect(
                    account=kwargs.get("account", ""),
                    user=kwargs.get("user", ""),
                    password=kwargs.get("password", ""),
                    warehouse=kwargs.get("warehouse"),
                    database=kwargs.get("database"),
                    schema=kwargs.get("schema"),
                    role=kwargs.get("role"),
                )
            except Exception as e:
                raise RuntimeError(f"Snowflake connection failed: {e}")
            try:
                _cur = _sf_conn.cursor()
                _sf_schema = (
                    schema_filter or kwargs.get("schema") or "PUBLIC"
                ).split(",")[0].strip().upper()
                _cur.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    (_sf_schema,),
                )
                _rows = _cur.fetchall()
                _cur.close()
            finally:
                try:
                    _sf_conn.close()
                except Exception:
                    pass
            _t = {}
            for r in _rows:
                _tbl = str(r[0])
                _t.setdefault(_tbl, []).append({
                    "name": str(r[1]),
                    "type": _normalize_type(str(r[2])),
                    "sample": "",
                    "nullable": str(r[3]).upper() == "YES",
                })
            _t = _apply_filters(_t, schema_filter, table_filter)
            return {"tables": [{"name": k, "columns": v} for k, v in _t.items()]}

    # ── Existing SQLAlchemy-based branches (untouched) ───────────────────────
    if db_type == "oracle":
        try:
            import cx_Oracle  # type: ignore
        except ImportError:
            try:
                import oracledb as cx_Oracle  # type: ignore
            except ImportError:
                raise RuntimeError(f"Driver for oracle not installed. Run: {_DB_INSTALL_HINTS['oracle']}")
        import sqlalchemy
        try:
            engine = sqlalchemy.create_engine(conn_str)
        except Exception as e:
            raise RuntimeError(f"Oracle connection failed: {e}")
        query = "SELECT table_name, column_name, data_type, nullable FROM all_tab_columns ORDER BY table_name, column_id"
        with engine.connect() as conn:
            rows = conn.execute(sqlalchemy.text(query)).fetchall()
        tables: Dict = {}
        for r in rows:
            tbl = str(r[0])
            if tbl not in tables:
                tables[tbl] = []
            tables[tbl].append({
                "name": str(r[1]),
                "type": _normalize_type(str(r[2])),
                "sample": "",
                "nullable": str(r[3]).upper() == "Y",
            })

    elif db_type == "snowflake":
        try:
            import snowflake.connector  # type: ignore
            import sqlalchemy
            engine = sqlalchemy.create_engine(conn_str)
            with engine.connect() as conn:
                rows = conn.execute(sqlalchemy.text(_INFOSYS_QUERY)).fetchall()
        except ImportError:
            raise RuntimeError(f"Driver for snowflake not installed. Run: {_DB_INSTALL_HINTS['snowflake']}")
        except Exception as e:
            raise RuntimeError(f"Snowflake connection failed: {e}")
        tables = {}
        for r in rows:
            tbl = str(r[0])
            if tbl not in tables:
                tables[tbl] = []
            tables[tbl].append({
                "name": str(r[1]),
                "type": _normalize_type(str(r[2])),
                "sample": "",
                "nullable": str(r[3]).upper() == "YES",
            })

    elif db_type == "databricks":
        try:
            from databricks import sql as dbsql  # type: ignore
        except ImportError:
            raise RuntimeError(f"Driver for databricks not installed. Run: {_DB_INSTALL_HINTS['databricks']}")
        try:
            import sqlalchemy
            engine = sqlalchemy.create_engine(conn_str)
            with engine.connect() as conn:
                rows = conn.execute(sqlalchemy.text("SHOW TABLES")).fetchall()
            tables = {}
            with engine.connect() as conn:
                for row in rows:
                    tbl = str(row[1]) if len(row) > 1 else str(row[0])
                    try:
                        if not re.match(r'^[\w.]+$', tbl):
                            continue
                        cols = conn.execute(sqlalchemy.text(f"DESCRIBE TABLE `{tbl}`")).fetchall()
                        tables[tbl] = [{"name": str(c[0]), "type": _normalize_type(str(c[1])), "sample": "", "nullable": True} for c in cols if c[0] and not str(c[0]).startswith("#")]
                    except Exception:
                        pass
        except Exception as e:
            raise RuntimeError(f"Databricks connection failed: {e}")

    else:
        try:
            import sqlalchemy
        except ImportError:
            raise RuntimeError("sqlalchemy not installed. Run: pip install sqlalchemy")

        try:
            engine = sqlalchemy.create_engine(conn_str)
        except Exception as e:
            raise RuntimeError(f"Could not create engine for {db_type}: {e}")

        try:
            with engine.connect() as conn:
                rows = conn.execute(sqlalchemy.text(_INFOSYS_QUERY)).fetchall()
        except Exception as e:
            install = _DB_INSTALL_HINTS.get(db_type, "")
            raise RuntimeError(f"Database query failed for {db_type}: {e}. Hint: {install}")

        tables = {}
        for r in rows:
            tbl = str(r[0])
            if tbl not in tables:
                tables[tbl] = []
            tables[tbl].append({
                "name": str(r[1]),
                "type": _normalize_type(str(r[2])),
                "sample": "",
                "nullable": str(r[3]).upper() == "YES",
            })

    if table_filter:
        allowed = {t.strip().lower() for t in table_filter.split(",") if t.strip()}
        tables = {k: v for k, v in tables.items() if k.lower() in allowed}

    if schema_filter:
        schemas = {s.strip().lower() for s in schema_filter.split(",") if s.strip()}
        filtered = {}
        for tbl, cols in tables.items():
            parts = tbl.split(".")
            if len(parts) == 2 and parts[0].lower() not in schemas:
                continue
            filtered[tbl] = cols
        if filtered:
            tables = filtered

    result_tables = [{"name": tbl, "columns": cols} for tbl, cols in tables.items()]
    return {"tables": result_tables}
