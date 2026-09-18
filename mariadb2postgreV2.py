#!/usr/bin/env python3

import argparse
import os
import re
import sys
import time
from pathlib import Path

import mariadb


# ============================================================
# CONFIG
# ============================================================

BATCH_SIZE = 1000

# Maksimal row dalam satu file SQL.
#
# Contoh:
#   100000 rows
#   -> part_0001.sql
#   -> part_0002.sql
#   -> part_0003.sql
#
MAX_ROWS_PER_FILE = 100_000


# ============================================================
# TABLE MIGRATION CONFIG
# ============================================================

# ------------------------------------------------------------
# EXCLUDE_TABLES
# ------------------------------------------------------------
#
# Table yang benar-benar TIDAK akan dimigrasikan.
#
# Tidak dibuat:
#   - schema
#   - data
#   - foreign key
#   - index
#
EXCLUDE_TABLES = {
    # "temporary_table",
    # "old_backup",
}


# ------------------------------------------------------------
# DATA_ONLY_EXCLUDE_TABLES
# ------------------------------------------------------------
#
# Schema tetap dibuat.
#
# Tetapi DATA tidak di-export.
#
# Cocok untuk table besar seperti:
#
#   audit_logs
#   activity_logs
#   application_logs
#
DATA_ONLY_EXCLUDE_TABLES = {
    # "audit_logs",
    # "activity_logs",
    # "large_history",
}


# ------------------------------------------------------------
# DATA_WHERE
# ------------------------------------------------------------
#
# Filter data per table.
#
# Hanya data yang memenuhi WHERE yang akan di-export.
#
# Contoh:
#
# DATA_WHERE = {
#     "orders": "created_at >= '2025-01-01'",
#     "users": "deleted_at IS NULL",
#     "logs": "created_at >= '2026-01-01'",
# }
#
# PERHATIAN:
# Value di sini dimasukkan langsung ke SQL.
# Hanya isi dengan SQL yang Anda kontrol sendiri.
#
DATA_WHERE = {
    # "orders": "created_at >= '2025-01-01'",
    # "users": "status = 'active'",
}


# ============================================================
# IDENTIFIER / LITERAL
# ============================================================

def pg_ident(name):
    """
    PostgreSQL identifier.
    """
    return '"' + str(name).replace('"', '""') + '"'


def mysql_ident(name):
    """
    MariaDB identifier.
    """
    return "`" + str(name).replace("`", "``") + "`"


def pg_literal(value):
    """
    Convert Python value menjadi PostgreSQL literal.
    """

    if value is None:
        return "NULL"

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):

        if value != value:
            return "NULL"

        if value == float("inf"):
            return "'Infinity'"

        if value == float("-inf"):
            return "'-Infinity'"

        return str(value)

    if isinstance(value, bytes):
        return "'\\\\x" + value.hex() + "'::bytea"

    value = str(value)

    value = value.replace("'", "''")

    return "'" + value + "'"


# ============================================================
# TYPE CONVERSION
# ============================================================

def parse_base_type(column_type):
    """
    Contoh:

    varchar(255)
    decimal(10,2) unsigned
    enum('a','b')
    """

    match = re.match(
        r"^\s*([a-zA-Z]+)",
        column_type
    )

    if not match:
        return column_type.lower()

    return match.group(1).lower()


def extract_parentheses(text):
    match = re.search(
        r"\((.*?)\)",
        text
    )

    if match:
        return match.group(1)

    return None


def extract_enum_values(column_type):
    """
    enum('a','b','c')
    """

    match = re.search(
        r"enum\s*\((.*)\)",
        column_type,
        re.IGNORECASE,
    )

    if not match:
        return []

    content = match.group(1)

    values = []

    pattern = re.compile(
        r"'((?:\\.|[^'])*)'"
    )

    for item in pattern.findall(content):

        item = item.replace(
            "\\'",
            "'"
        )

        item = item.replace(
            "\\\\",
            "\\"
        )

        values.append(item)

    return values


def extract_set_values(column_type):
    """
    set('a','b','c')
    """

    return extract_enum_values(
        column_type
    )


def is_unsigned(column_type):
    return "unsigned" in column_type.lower()


def convert_integer_type(
    base_type,
    unsigned,
):

    if base_type == "tinyint":
        return "SMALLINT"

    if base_type == "smallint":
        return (
            "INTEGER"
            if unsigned
            else "SMALLINT"
        )

    if base_type == "mediumint":
        return (
            "BIGINT"
            if unsigned
            else "INTEGER"
        )

    if base_type in (
        "int",
        "integer",
    ):
        return (
            "BIGINT"
            if unsigned
            else "INTEGER"
        )

    if base_type == "bigint":
        return "BIGINT"

    return None


def convert_type(
    column_name,
    column_type,
):
    """
    MariaDB -> PostgreSQL
    """

    base = parse_base_type(
        column_type
    )

    unsigned = is_unsigned(
        column_type
    )

    # --------------------------------------------------------
    # BOOLEAN
    # --------------------------------------------------------
    # bagian dibawah ini di remarks karena menjadikan tinyint(1) -> boolean
    ##if base == "tinyint":

        ##size = extract_parentheses(
            ##column_type
        ##)

        ##if size == "1":
            ##return "BOOLEAN", None

    # --------------------------------------------------------
    # ENUM
    # --------------------------------------------------------

    if base == "enum":

        values = extract_enum_values(
            column_type
        )

        return "VARCHAR(255)", {
            "kind": "enum",
            "values": values,
        }

    # --------------------------------------------------------
    # SET
    # --------------------------------------------------------

    if base == "set":

        values = extract_set_values(
            column_type
        )

        return "TEXT", {
            "kind": "set",
            "values": values,
        }

    # --------------------------------------------------------
    # INTEGER
    # --------------------------------------------------------

    integer_types = {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "integer",
        "bigint",
    }

    if base in integer_types:

        return (
            convert_integer_type(
                base,
                unsigned,
            ),
            None,
        )

    # --------------------------------------------------------
    # DECIMAL / NUMERIC
    # --------------------------------------------------------

    if base in (
        "decimal",
        "numeric",
    ):

        size = extract_parentheses(
            column_type
        )

        if size:
            return (
                f"NUMERIC({size})",
                None,
            )

        return "NUMERIC", None

    # --------------------------------------------------------
    # FLOAT
    # --------------------------------------------------------

    if base == "float":
        return "REAL", None

    # --------------------------------------------------------
    # DOUBLE
    # --------------------------------------------------------

    if base in (
        "double",
        "real",
    ):

        return (
            "DOUBLE PRECISION",
            None,
        )

    # --------------------------------------------------------
    # CHAR
    # --------------------------------------------------------

    if base == "char":

        size = extract_parentheses(
            column_type
        )

        if size:
            return (
                f"CHAR({size})",
                None,
            )

        return "CHAR", None

    # --------------------------------------------------------
    # VARCHAR
    # --------------------------------------------------------

    if base == "varchar":

        size = extract_parentheses(
            column_type
        )

        if size:
            return (
                f"VARCHAR({size})",
                None,
            )

        return "VARCHAR", None

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if base in (
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
    ):

        return "TEXT", None

    # --------------------------------------------------------
    # BINARY
    # --------------------------------------------------------

    if base in (
        "binary",
        "varbinary",
        "blob",
        "tinyblob",
        "mediumblob",
        "longblob",
    ):

        return "BYTEA", None

    # --------------------------------------------------------
    # DATE / TIME
    # --------------------------------------------------------

    if base == "date":
        return "DATE", None

    if base in (
        "datetime",
        "timestamp",
    ):

        return "TIMESTAMP", None

    if base == "time":
        return "TIME", None

    if base == "year":
        return "INTEGER", None

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if base == "json":
        return "JSONB", None

    # --------------------------------------------------------
    # BOOLEAN
    # --------------------------------------------------------

    if base in (
        "boolean",
        "bool",
    ):

        return "BOOLEAN", None

    # --------------------------------------------------------
    # BIT
    # --------------------------------------------------------

    if base == "bit":

        size = extract_parentheses(
            column_type
        )

        if size == "1":
            return "BOOLEAN", None

        if size:
            return (
                f"BIT({size})",
                None,
            )

        return "BIT", None

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    print(
        f"[WARNING] Unknown MariaDB type "
        f"{column_type} pada {column_name}; "
        f"menggunakan TEXT",
        file=sys.stderr,
    )

    return "TEXT", None


# ============================================================
# DATABASE METADATA
# ============================================================

def get_tables(cursor):

    cursor.execute(
        """
        SELECT
            TABLE_NAME,
            TABLE_TYPE
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )

    return cursor.fetchall()


def get_columns(
    cursor,
    table,
):

    cursor.execute(
        """
        SELECT
            COLUMN_NAME,
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            EXTRA,
            ORDINAL_POSITION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )

    return cursor.fetchall()


def get_primary_key(
    cursor,
    table,
):

    cursor.execute(
        """
        SELECT
            COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = ?
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )

    return [
        row[0]
        for row in cursor.fetchall()
    ]


def get_foreign_keys(
    cursor,
    table,
):

    cursor.execute(
        """
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            rc.UPDATE_RULE,
            rc.DELETE_RULE
        FROM information_schema.KEY_COLUMN_USAGE kcu

        JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
          ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
         AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
         AND rc.TABLE_NAME = kcu.TABLE_NAME

        WHERE kcu.CONSTRAINT_SCHEMA = DATABASE()
          AND kcu.TABLE_NAME = ?
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL

        ORDER BY
            kcu.CONSTRAINT_NAME,
            kcu.ORDINAL_POSITION
        """,
        (table,),
    )

    return cursor.fetchall()


def get_indexes(
    cursor,
    table,
):

    cursor.execute(
        """
        SELECT
            INDEX_NAME,
            NON_UNIQUE,
            COLUMN_NAME,
            SEQ_IN_INDEX,
            INDEX_TYPE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = ?
          AND INDEX_NAME != 'PRIMARY'
        ORDER BY
            INDEX_NAME,
            SEQ_IN_INDEX
        """,
        (table,),
    )

    indexes = {}

    for row in cursor.fetchall():

        name = row[0]
        non_unique = row[1]
        column = row[2]
        index_type = row[4]

        if name not in indexes:

            indexes[name] = {
                "unique": non_unique == 0,
                "columns": [],
                "type": index_type,
            }

        indexes[name]["columns"].append(
            column
        )

    return indexes


# ============================================================
# TABLE ROW COUNT
# ============================================================

def get_table_row_count(
    cursor,
    table,
):
    """
    Exact COUNT(*).

    Jika DATA_WHERE tersedia,
    count hanya row yang akan di-export.
    """

    where = DATA_WHERE.get(
        table
    )

    sql = (
        f"SELECT COUNT(*) "
        f"FROM {mysql_ident(table)}"
    )

    if where:
        sql += f" WHERE {where}"

    cursor.execute(sql)

    result = cursor.fetchone()

    return int(result[0])


def get_estimated_table_row_count(
    cursor,
    table,
):
    """
    Mengambil estimasi TABLE_ROWS dari
    information_schema.

    Untuk InnoDB nilainya bisa approximate.
    Digunakan hanya untuk informasi.
    """

    cursor.execute(
        """
        SELECT TABLE_ROWS
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = ?
        """,
        (table,),
    )

    row = cursor.fetchone()

    if not row:
        return 0

    if row[0] is None:
        return 0

    return int(row[0])


# ============================================================
# DEFAULT CONVERSION
# ============================================================

def convert_default(
    default_value,
    pg_type,
):

    if default_value is None:
        return None

    value = str(default_value)

    upper = value.upper()

    if upper in (
        "CURRENT_TIMESTAMP",
        "CURRENT_TIMESTAMP()",
        "NOW()",
    ):

        return "CURRENT_TIMESTAMP"

    if upper == "CURRENT_DATE":
        return "CURRENT_DATE"

    if upper == "NULL":
        return "NULL"

    if re.fullmatch(
        r"-?\d+(\.\d+)?",
        value,
    ):

        return value

    if pg_type == "BOOLEAN":

        if value in (
            "1",
            "TRUE",
            "true",
        ):

            return "TRUE"

        if value in (
            "0",
            "FALSE",
            "false",
        ):

            return "FALSE"

    return pg_literal(value)


# ============================================================
# CHECK CONSTRAINT
# ============================================================

def generate_enum_check(
    table,
    column,
    values,
):

    if not values:
        return ""

    values_sql = ", ".join(
        pg_literal(v)
        for v in values
    )

    return (
        f"CONSTRAINT "
        f"{pg_ident('chk_' + table + '_' + column)} "
        f"CHECK ({pg_ident(column)} "
        f"IN ({values_sql}))"
    )


def generate_set_check(
    table,
    column,
    values,
):

    # PostgreSQL CHECK constraint
    # tidak boleh menggunakan subquery.
    #
    # Karena itu SET dikonversi menjadi TEXT
    # tanpa CHECK otomatis.

    return ""


# ============================================================
# CREATE TABLE
# ============================================================

def generate_create_table(
    cursor,
    table,
):

    columns = get_columns(
        cursor,
        table,
    )

    if not columns:
        return ""

    primary_keys = get_primary_key(
        cursor,
        table,
    )

    lines = []
    check_constraints = []

    for row in columns:

        (
            column_name,
            column_type,
            nullable,
            default_value,
            extra,
            ordinal,
        ) = row

        pg_type, metadata = convert_type(
            column_name,
            column_type,
        )

        extra_lower = (
            extra or ""
        ).lower()

        line = (
            f"{pg_ident(column_name)} "
            f"{pg_type}"
        )

        # ----------------------------------------------------
        # AUTO_INCREMENT
        # ----------------------------------------------------

        if "auto_increment" in extra_lower:

            line += (
                " GENERATED BY DEFAULT "
                "AS IDENTITY"
            )

        # ----------------------------------------------------
        # NULL
        # ----------------------------------------------------

        if nullable == "NO":
            line += " NOT NULL"

        # ----------------------------------------------------
        # DEFAULT
        # ----------------------------------------------------

        if (
            default_value is not None
            and "auto_increment"
            not in extra_lower
        ):

            default_sql = convert_default(
                default_value,
                pg_type,
            )

            if default_sql:

                line += (
                    f" DEFAULT {default_sql}"
                )

        lines.append(line)

        # ----------------------------------------------------
        # ENUM CHECK
        # ----------------------------------------------------

        if (
            metadata
            and metadata.get("kind") == "enum"
        ):

            check = generate_enum_check(
                table,
                column_name,
                metadata["values"],
            )

            if check:
                check_constraints.append(
                    check
                )

    # --------------------------------------------------------
    # PRIMARY KEY
    # --------------------------------------------------------

    if primary_keys:

        pk_columns = ", ".join(
            pg_ident(c)
            for c in primary_keys
        )

        lines.append(
            f"CONSTRAINT "
            f"{pg_ident('pk_' + table)} "
            f"PRIMARY KEY ({pk_columns})"
        )

    # --------------------------------------------------------
    # CHECK
    # --------------------------------------------------------

    lines.extend(
        check_constraints
    )

    sql = (
        f"CREATE TABLE {pg_ident(table)} (\n"
        + ",\n".join(
            "    " + line
            for line in lines
        )
        + "\n);\n"
    )

    return sql


# ============================================================
# FOREIGN KEYS
# ============================================================

def generate_foreign_keys(
    cursor,
    table,
):

    foreign_keys = get_foreign_keys(
        cursor,
        table,
    )

    output = []

    for (
        constraint_name,
        column_name,
        referenced_table,
        referenced_column,
        update_rule,
        delete_rule,
    ) in foreign_keys:

        # Jangan generate FK menuju table yang
        # sengaja di-exclude.
        if referenced_table in EXCLUDE_TABLES:

            print(
                f"[WARNING] FK {constraint_name} "
                f"pada {table} mengarah ke "
                f"excluded table {referenced_table}; "
                f"FK dilewati.",
                file=sys.stderr,
            )

            continue

        sql = (
            f"ALTER TABLE {pg_ident(table)} "
            f"ADD CONSTRAINT "
            f"{pg_ident(constraint_name)} "
            f"FOREIGN KEY "
            f"({pg_ident(column_name)}) "
            f"REFERENCES "
            f"{pg_ident(referenced_table)} "
            f"({pg_ident(referenced_column)})"
        )

        if update_rule:
            sql += (
                f" ON UPDATE "
                f"{update_rule}"
            )

        if delete_rule:
            sql += (
                f" ON DELETE "
                f"{delete_rule}"
            )

        sql += ";"

        output.append(sql)

    if not output:
        return ""

    return "\n".join(output) + "\n"


# ============================================================
# INDEXES
# ============================================================

def generate_indexes(
    cursor,
    table,
):

    indexes = get_indexes(
        cursor,
        table,
    )

    output = []

    for index_name, info in indexes.items():

        columns = ", ".join(
            pg_ident(c)
            for c in info["columns"]
        )

        unique = (
            "UNIQUE "
            if info["unique"]
            else ""
        )

        if info["type"].upper() in (
            "FULLTEXT",
            "SPATIAL",
        ):

            print(
                f"[WARNING] Index {index_name} "
                f"({info['type']}) pada {table} "
                f"tidak dikonversi otomatis.",
                file=sys.stderr,
            )

            continue

        output.append(
            f"CREATE {unique}INDEX "
            f"{pg_ident(index_name)} "
            f"ON {pg_ident(table)} "
            f"({columns});"
        )

    if not output:
        return ""

    return "\n".join(output) + "\n"


# ============================================================
# DATA
# ============================================================

def sanitize_filename(name):
    """
    Membuat nama table aman digunakan sebagai nama folder/file.
    """

    result = re.sub(
        r"[^a-zA-Z0-9_.-]+",
        "_",
        str(name),
    )

    if not result:
        result = "table"

    return result


def build_data_query(
    table,
):

    where = DATA_WHERE.get(
        table
    )

    sql = (
        f"SELECT * "
        f"FROM {mysql_ident(table)}"
    )

    if where:
        sql += f" WHERE {where}"

    return sql


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"

    minutes = int(seconds // 60)
    remaining = seconds % 60

    if minutes < 60:
        return (
            f"{minutes}m "
            f"{remaining:.0f}s"
        )

    hours = int(minutes // 60)
    minutes = minutes % 60

    return (
        f"{hours}h "
        f"{minutes}m"
    )


def write_insert_batch(
    output,
    table,
    columns_sql,
    rows,
):
    """
    Generate satu INSERT statement.
    """

    values_list = []

    for row in rows:

        values = ", ".join(
            pg_literal(v)
            for v in row
        )

        values_list.append(
            f"({values})"
        )

    output.write(
        f"INSERT INTO "
        f"{pg_ident(table)} "
        f"({columns_sql}) VALUES\n"
    )

    output.write(
        ",\n".join(values_list)
    )

    output.write(
        ";\n\n"
    )


def generate_data(
    cursor,
    table,
    data_dir,
    batch_size=BATCH_SIZE,
    max_rows_per_file=MAX_ROWS_PER_FILE,
    expected_rows=None,
):
    """
    Streaming/batch data export.

    Output:

        data/
          table_name/
            part_0001.sql
            part_0002.sql
            part_0003.sql
    """

    table_dir = (
        Path(data_dir)
        / sanitize_filename(table)
    )

    table_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    query = build_data_query(
        table
    )

    cursor.execute(query)

    columns = [
        description[0]
        for description
        in cursor.description
    ]

    columns_sql = ", ".join(
        pg_ident(c)
        for c in columns
    )

    total_exported = 0
    file_row_count = 0
    part_number = 0

    current_file = None
    current_path = None

    started_at = time.monotonic()
    last_progress = started_at

    def open_new_file():

        nonlocal current_file
        nonlocal current_path
        nonlocal part_number
        nonlocal file_row_count

        if current_file:
            current_file.write(
                "COMMIT;\n"
            )

            current_file.close()

        part_number += 1

        current_path = (
            table_dir
            / f"part_{part_number:04d}.sql"
        )

        current_file = open(
            current_path,
            "w",
            encoding="utf-8",
        )

        current_file.write(
            "-- MariaDB -> PostgreSQL\n"
        )

        current_file.write(
            f"-- TABLE: {table}\n"
        )

        current_file.write(
            f"-- PART: {part_number}\n"
        )

        if table in DATA_WHERE:
            current_file.write(
                f"-- WHERE: "
                f"{DATA_WHERE[table]}\n"
            )

        current_file.write(
            "\nBEGIN;\n\n"
        )

        file_row_count = 0

    try:

        while True:

            rows = cursor.fetchmany(
                batch_size
            )

            if not rows:
                break

            row_offset = 0

            while row_offset < len(rows):

                if (
                    current_file is None
                    or file_row_count
                    >= max_rows_per_file
                ):

                    open_new_file()

                remaining = (
                    max_rows_per_file
                    - file_row_count
                )

                chunk = rows[
                    row_offset:
                    row_offset + remaining
                ]

                write_insert_batch(
                    current_file,
                    table,
                    columns_sql,
                    chunk,
                )

                chunk_count = len(chunk)

                file_row_count += (
                    chunk_count
                )

                total_exported += (
                    chunk_count
                )

                row_offset += (
                    chunk_count
                )

                now = time.monotonic()

                if (
                    now - last_progress >= 1.0
                    or (
                        expected_rows
                        and total_exported
                        >= expected_rows
                    )
                ):

                    elapsed = (
                        now - started_at
                    )

                    rate = (
                        total_exported
                        / elapsed
                        if elapsed > 0
                        else 0
                    )

                    if expected_rows:

                        percent = (
                            total_exported
                            / expected_rows
                            * 100
                        )

                        if percent > 100:
                            percent = 100

                        progress = (
                            f"{total_exported:,}/"
                            f"{expected_rows:,}"
                            f" ({percent:.1f}%)"
                        )

                    else:

                        progress = (
                            f"{total_exported:,}"
                        )

                    print(
                        f"    progress: "
                        f"{progress} | "
                        f"{rate:,.0f} rows/s | "
                        f"elapsed "
                        f"{format_duration(elapsed)}"
                    )

                    last_progress = now

    finally:

        if current_file:

            current_file.write(
                "COMMIT;\n"
            )

            current_file.close()

    elapsed = (
        time.monotonic()
        - started_at
    )

    rate = (
        total_exported / elapsed
        if elapsed > 0
        else 0
    )

    print(
        f"    exported: "
        f"{total_exported:,} rows | "
        f"{part_number} file(s) | "
        f"{rate:,.0f} rows/s | "
        f"elapsed "
        f"{format_duration(elapsed)}"
    )

    return {
        "rows": total_exported,
        "parts": part_number,
        "directory": str(table_dir),
        "elapsed": elapsed,
    }


# ============================================================
# VIEWS
# ============================================================

def get_views(cursor):

    cursor.execute(
        """
        SELECT
            TABLE_NAME,
            VIEW_DEFINITION,
            CHECK_OPTION
        FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )

    return cursor.fetchall()


def convert_view_sql(sql):

    if not sql:
        return sql

    result = sql

    result = re.sub(
        r"`([^`]+)`",
        r'"\1"',
        result,
    )

    result = re.sub(
        r"\bIFNULL\s*\(",
        "COALESCE(",
        result,
        flags=re.IGNORECASE,
    )

    result = re.sub(
        r"\bNOW\s*\(\s*\)",
        "CURRENT_TIMESTAMP",
        result,
        flags=re.IGNORECASE,
    )

    if re.search(
        r"\bDATE_FORMAT\s*\(",
        result,
        flags=re.IGNORECASE,
    ):

        print(
            "[WARNING] View menggunakan "
            "DATE_FORMAT(). "
            "Perlu konversi manual ke TO_CHAR().",
            file=sys.stderr,
        )

    return result


def generate_views(cursor):

    views = get_views(cursor)

    output = []

    for (
        view_name,
        definition,
        check_option,
    ) in views:

        sql = convert_view_sql(
            definition
        )

        if not sql:
            continue

        output.append(
            f"CREATE OR REPLACE VIEW "
            f"{pg_ident(view_name)} AS\n"
            f"{sql.strip()};\n"
        )

    return "\n".join(output)


# ============================================================
# TRIGGERS
# ============================================================

def get_triggers(cursor):

    cursor.execute(
        """
        SELECT
            TRIGGER_NAME,
            EVENT_MANIPULATION,
            EVENT_OBJECT_TABLE,
            ACTION_TIMING,
            ACTION_STATEMENT
        FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = DATABASE()
        ORDER BY EVENT_OBJECT_TABLE,
                 TRIGGER_NAME
        """
    )

    return cursor.fetchall()


def convert_trigger_statement(
    statement
):

    result = statement.strip()

    result = re.sub(
        r"\bBEGIN\s*",
        "BEGIN\n",
        result,
        flags=re.IGNORECASE,
    )

    result = re.sub(
        r"\bIFNULL\s*\(",
        "COALESCE(",
        result,
        flags=re.IGNORECASE,
    )

    return result


def generate_triggers(cursor):

    triggers = get_triggers(
        cursor
    )

    output = []

    for row in triggers:

        (
            trigger_name,
            event,
            table,
            timing,
            statement,
        ) = row

        print(
            f"[WARNING] Trigger "
            f"{trigger_name} pada {table} "
            f"ditemukan. "
            f"Konversi otomatis trigger kompleks "
            f"mungkin membutuhkan review manual.",
            file=sys.stderr,
        )

        function_name = (
            f"fn_{trigger_name}"
        )

        body = convert_trigger_statement(
            statement
        )

        function_sql = f"""
CREATE OR REPLACE FUNCTION
{pg_ident(function_name)}()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN

    {body}

    RETURN NEW;
END;
$$;
"""

        trigger_sql = f"""
DROP TRIGGER IF EXISTS
{pg_ident(trigger_name)}
ON {pg_ident(table)};

CREATE TRIGGER
{pg_ident(trigger_name)}
{timing} {event}
ON {pg_ident(table)}
FOR EACH ROW
EXECUTE FUNCTION
{pg_ident(function_name)}();
"""

        output.append(
            function_sql
            + trigger_sql
        )

    return "\n".join(output)


# ============================================================
# ON UPDATE CURRENT_TIMESTAMP
# ============================================================

def find_on_update_columns(
    cursor,
):

    cursor.execute(
        """
        SELECT
            TABLE_NAME,
            COLUMN_NAME,
            COLUMN_TYPE,
            EXTRA
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND EXTRA LIKE '%on update%'
        ORDER BY TABLE_NAME,
                 ORDINAL_POSITION
        """
    )

    return cursor.fetchall()


def generate_on_update_triggers(
    cursor,
):

    columns = find_on_update_columns(
        cursor
    )

    grouped = {}

    for (
        table,
        column,
        column_type,
        extra,
    ) in columns:

        if table in EXCLUDE_TABLES:
            continue

        grouped.setdefault(
            table,
            []
        ).append(column)

    output = []

    for table, column_list in grouped.items():

        function_name = (
            f"fn_{table}_on_update"
        )

        assignments = []

        for column in column_list:

            assignments.append(
                f"    NEW.{pg_ident(column)} "
                f"= CURRENT_TIMESTAMP;"
            )

        function_sql = f"""
CREATE OR REPLACE FUNCTION
{pg_ident(function_name)}()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
{chr(10).join(assignments)}

    RETURN NEW;
END;
$$;
"""

        trigger_name = (
            f"trg_{table}_on_update"
        )

        trigger_sql = f"""
DROP TRIGGER IF EXISTS
{pg_ident(trigger_name)}
ON {pg_ident(table)};

CREATE TRIGGER
{pg_ident(trigger_name)}
BEFORE UPDATE
ON {pg_ident(table)}
FOR EACH ROW
EXECUTE FUNCTION
{pg_ident(function_name)}();
"""

        output.append(
            function_sql
            + trigger_sql
        )

    return "\n".join(output)


# ============================================================
# STORED PROCEDURES / FUNCTIONS
# ============================================================

def get_routines(cursor):

    cursor.execute(
        """
        SELECT
            ROUTINE_NAME,
            ROUTINE_TYPE,
            DATA_TYPE,
            ROUTINE_DEFINITION
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = DATABASE()
        ORDER BY ROUTINE_NAME
        """
    )

    return cursor.fetchall()


def generate_routines(cursor):

    routines = get_routines(
        cursor
    )

    output = []

    for (
        name,
        routine_type,
        data_type,
        definition,
    ) in routines:

        print(
            f"[WARNING] Stored routine "
            f"{name} ({routine_type}) ditemukan. "
            f"Konversi otomatis hanya basic "
            f"dan perlu review manual.",
            file=sys.stderr,
        )

        if not definition:
            continue

        body = definition.strip()

        body = re.sub(
            r"\bIFNULL\s*\(",
            "COALESCE(",
            body,
            flags=re.IGNORECASE,
        )

        body = re.sub(
            r"\bNOW\s*\(\s*\)",
            "CURRENT_TIMESTAMP",
            body,
            flags=re.IGNORECASE,
        )

        pg_return = "VOID"

        if routine_type.upper() == "FUNCTION":

            if data_type:

                pg_return, _ = convert_type(
                    name,
                    data_type,
                )

        sql = f"""
-- TODO: Review routine {name}
-- Original MariaDB routine body:
-- {body.replace(chr(10), chr(10) + '-- ')}

-- CREATE OR REPLACE FUNCTION
-- {pg_ident(name)}(...)
-- RETURNS {pg_return}
-- LANGUAGE plpgsql
-- AS $$
-- BEGIN
--     ...
-- END;
-- $$;
"""

        output.append(sql)

    return "\n".join(output)


# ============================================================
# MANIFEST
# ============================================================

def write_manifest(
    manifest_file,
    results,
):

    with open(
        manifest_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "MariaDB -> PostgreSQL "
            "Data Export Manifest\n"
        )

        f.write(
            "=" * 80
            + "\n\n"
        )

        for item in results:

            f.write(
                f"TABLE       : "
                f"{item['table']}\n"
            )

            f.write(
                f"STATUS      : "
                f"{item['status']}\n"
            )

            f.write(
                f"ROW COUNT   : "
                f"{item.get('row_count', '-')}\n"
            )

            f.write(
                f"EXPORTED    : "
                f"{item.get('exported_rows', '-')}\n"
            )

            f.write(
                f"PARTS       : "
                f"{item.get('parts', '-')}\n"
            )

            f.write(
                f"DATA WHERE  : "
                f"{item.get('where', '-')}\n"
            )

            f.write(
                f"PATH        : "
                f"{item.get('path', '-')}\n"
            )

            f.write(
                "\n"
            )


# ============================================================
# DATABASE MIGRATION
# ============================================================

def migrate_database(
    host,
    port,
    user,
    password,
    database,
    output_dir,
    batch_size,
    max_rows_per_file,
):

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    ddl_file = (
        output_dir
        / "01_schema.sql"
    )

    fk_file = (
        output_dir
        / "02_foreign_keys.sql"
    )

    index_file = (
        output_dir
        / "03_indexes.sql"
    )

    data_dir = (
        output_dir
        / "04_data"
    )

    view_file = (
        output_dir
        / "05_views.sql"
    )

    trigger_file = (
        output_dir
        / "06_triggers.sql"
    )

    routine_file = (
        output_dir
        / "07_routines_manual.sql"
    )

    manifest_file = (
        output_dir
        / "04_data_manifest.txt"
    )

    print()
    print("=" * 70)
    print("MariaDB -> PostgreSQL Migration")
    print("=" * 70)
    print()
    print(f"Database         : {database}")
    print(f"Output           : {output_dir}")
    print(f"Batch size       : {batch_size:,}")
    print(
        f"Rows / SQL file : "
        f"{max_rows_per_file:,}"
    )
    print()

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    conn = mariadb.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )

    cursor = conn.cursor()

    tables = get_tables(
        cursor
    )

    all_base_tables = [
        table
        for table, table_type
        in tables
        if table_type == "BASE TABLE"
    ]

    excluded_tables = [
        table
        for table in all_base_tables
        if table in EXCLUDE_TABLES
    ]

    normal_tables = [
        table
        for table in all_base_tables
        if table not in EXCLUDE_TABLES
    ]

    print(
        f"[+] Base tables        : "
        f"{len(all_base_tables)}"
    )

    print(
        f"[+] Excluded tables    : "
        f"{len(excluded_tables)}"
    )

    print(
        f"[+] Migrated tables    : "
        f"{len(normal_tables)}"
    )

    if excluded_tables:

        print()

        print(
            "[EXCLUDED]"
        )

        for table in excluded_tables:

            print(
                f"    - {table}"
            )

    print()

    # --------------------------------------------------------
    # SCHEMA
    # --------------------------------------------------------

    print(
        "[1/7] Generating schema..."
    )

    with open(
        ddl_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- MariaDB -> PostgreSQL\n"
        )

        f.write(
            f"-- Database: {database}\n\n"
        )

        f.write(
            "BEGIN;\n\n"
        )

        for table in normal_tables:

            print(
                f"[SCHEMA] {table}"
            )

            f.write(
                "-- ========================================\n"
            )

            f.write(
                f"-- TABLE: {table}\n"
            )

            f.write(
                "-- ========================================\n\n"
            )

            f.write(
                generate_create_table(
                    cursor,
                    table,
                )
            )

            f.write(
                "\n"
            )

        f.write(
            "COMMIT;\n"
        )

    # --------------------------------------------------------
    # FOREIGN KEYS
    # --------------------------------------------------------

    print()
    print(
        "[2/7] Generating foreign keys..."
    )

    with open(
        fk_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Foreign Keys\n\n"
        )

        for table in normal_tables:

            sql = generate_foreign_keys(
                cursor,
                table,
            )

            if sql:

                f.write(sql)
                f.write("\n")

    # --------------------------------------------------------
    # INDEXES
    # --------------------------------------------------------

    print(
        "[3/7] Generating indexes..."
    )

    with open(
        index_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Indexes\n\n"
        )

        for table in normal_tables:

            sql = generate_indexes(
                cursor,
                table,
            )

            if sql:

                f.write(sql)
                f.write("\n")

    # --------------------------------------------------------
    # DATA
    # --------------------------------------------------------

    print()
    print(
        "[4/7] Exporting data..."
    )
    print()

    data_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_results = []

    for index, table in enumerate(
        normal_tables,
        start=1,
    ):

        print(
            "-" * 70
        )

        print(
            f"[DATA {index}/"
            f"{len(normal_tables)}] "
            f"{table}"
        )

        # ----------------------------------------------------
        # DATA ONLY EXCLUDED
        # ----------------------------------------------------

        if table in DATA_ONLY_EXCLUDE_TABLES:

            estimated_rows = (
                get_estimated_table_row_count(
                    cursor,
                    table,
                )
            )

            print(
                f"    status: SKIP DATA"
            )

            print(
                f"    estimated rows: "
                f"{estimated_rows:,}"
            )

            manifest_results.append({
                "table": table,
                "status": "SKIPPED_DATA",
                "row_count": (
                    f"~{estimated_rows:,}"
                ),
                "exported_rows": 0,
                "parts": 0,
                "where": (
                    DATA_WHERE.get(
                        table,
                        "-"
                    )
                ),
                "path": "-",
            })

            continue

        # ----------------------------------------------------
        # EXACT ROW COUNT
        # ----------------------------------------------------

        print(
            "    counting rows..."
        )

        count_started = time.monotonic()

        row_count = get_table_row_count(
            cursor,
            table,
        )

        count_elapsed = (
            time.monotonic()
            - count_started
        )

        print(
            f"    row count: "
            f"{row_count:,}"
            f" "
            f"(count: "
            f"{format_duration(count_elapsed)})"
        )

        # ----------------------------------------------------
        # EMPTY TABLE
        # ----------------------------------------------------

        if row_count == 0:

            print(
                "    status: EMPTY"
            )

            manifest_results.append({
                "table": table,
                "status": "EMPTY",
                "row_count": 0,
                "exported_rows": 0,
                "parts": 0,
                "where": (
                    DATA_WHERE.get(
                        table,
                        "-"
                    )
                ),
                "path": "-",
            })

            continue

        # ----------------------------------------------------
        # DATA EXPORT
        # ----------------------------------------------------

        result = generate_data(
            cursor=cursor,
            table=table,
            data_dir=data_dir,
            batch_size=batch_size,
            max_rows_per_file=max_rows_per_file,
            expected_rows=row_count,
        )

        manifest_results.append({
            "table": table,
            "status": "EXPORTED",
            "row_count": row_count,
            "exported_rows": result["rows"],
            "parts": result["parts"],
            "where": (
                DATA_WHERE.get(
                    table,
                    "-"
                )
            ),
            "path": result["directory"],
        })

    write_manifest(
        manifest_file,
        manifest_results,
    )

    # --------------------------------------------------------
    # VIEWS
    # --------------------------------------------------------

    print()
    print(
        "[5/7] Generating views..."
    )

    with open(
        view_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Views\n\n"
        )

        f.write(
            generate_views(
                cursor
            )
        )

    # --------------------------------------------------------
    # TRIGGERS
    # --------------------------------------------------------

    print(
        "[6/7] Generating triggers..."
    )

    with open(
        trigger_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Triggers\n\n"
        )

        f.write(
            "-- ========================================\n"
        )

        f.write(
            "-- ON UPDATE CURRENT_TIMESTAMP\n"
        )

        f.write(
            "-- ========================================\n\n"
        )

        f.write(
            generate_on_update_triggers(
                cursor
            )
        )

        f.write(
            "\n\n"
        )

        f.write(
            "-- ========================================\n"
        )

        f.write(
            "-- MariaDB Triggers\n"
        )

        f.write(
            "-- ========================================\n\n"
        )

        f.write(
            generate_triggers(
                cursor
            )
        )

    # --------------------------------------------------------
    # ROUTINES
    # --------------------------------------------------------

    print(
        "[7/7] Generating routines..."
    )

    with open(
        routine_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Stored Procedures / Functions\n"
        )

        f.write(
            "-- Review manually before execution.\n\n"
        )

        f.write(
            generate_routines(
                cursor
            )
        )

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    cursor.close()
    conn.close()

    print()
    print("=" * 70)
    print("Migration SQL berhasil dibuat")
    print("=" * 70)
    print()
    print(
        f"Schema    : {ddl_file}"
    )

    print(
        f"FK        : {fk_file}"
    )

    print(
        f"Index     : {index_file}"
    )

    print(
        f"Data      : {data_dir}"
    )

    print(
        f"Manifest  : {manifest_file}"
    )

    print(
        f"Views     : {view_file}"
    )

    print(
        f"Trigger   : {trigger_file}"
    )

    print(
        f"Routine   : {routine_file}"
    )

    print()


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Convert database MariaDB "
            "menjadi PostgreSQL SQL "
            "dengan streaming/batch data."
        )
    )

    parser.add_argument(
        "--host",
        default="localhost",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=3306,
    )

    parser.add_argument(
        "--user",
        required=True,
    )

    parser.add_argument(
        "--password",
        required=True,
    )

    parser.add_argument(
        "--database",
        required=True,
    )

    parser.add_argument(
        "--output-dir",
        default="./postgresql_dump_useradmin",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=(
            "Jumlah row yang dibaca "
            "per batch dari MariaDB."
        ),
    )

    parser.add_argument(
        "--max-rows-per-file",
        type=int,
        default=MAX_ROWS_PER_FILE,
        help=(
            "Jumlah maksimum row "
            "dalam satu file data SQL."
        ),
    )

    args = parser.parse_args()

    if args.batch_size <= 0:

        print(
            "[ERROR] --batch-size harus > 0",
            file=sys.stderr,
        )

        sys.exit(1)

    if args.max_rows_per_file <= 0:

        print(
            "[ERROR] "
            "--max-rows-per-file harus > 0",
            file=sys.stderr,
        )

        sys.exit(1)

    try:

        migrate_database(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            max_rows_per_file=(
                args.max_rows_per_file
            ),
        )

    except mariadb.Error as e:

        print(
            f"[ERROR] MariaDB: {e}",
            file=sys.stderr,
        )

        sys.exit(1)

    except KeyboardInterrupt:

        print(
            "\n[ERROR] Migration dihentikan "
            "oleh user.",
            file=sys.stderr,
        )

        sys.exit(130)

    except Exception as e:

        print(
            f"[ERROR] {e}",
            file=sys.stderr,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
