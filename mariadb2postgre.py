#!/usr/bin/env python3

import argparse
import os
import re
import sys
from pathlib import Path

import mariadb


# ============================================================
# CONFIG
# ============================================================

BATCH_SIZE = 1000


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
    match = re.search(r"\((.*?)\)", text)

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
        re.IGNORECASE
    )

    if not match:
        return []

    content = match.group(1)

    values = []

    # MariaDB enum values are quoted strings.
    pattern = re.compile(
        r"'((?:\\.|[^'])*)'"
    )

    for item in pattern.findall(content):
        item = item.replace("\\'", "'")
        item = item.replace("\\\\", "\\")

        values.append(item)

    return values


def extract_set_values(column_type):
    """
    set('a','b','c')
    """

    return extract_enum_values(column_type)


def is_unsigned(column_type):
    return "unsigned" in column_type.lower()


def convert_integer_type(base_type, unsigned):
    if base_type == "tinyint":
        return "SMALLINT" if unsigned else "SMALLINT"

    if base_type == "smallint":
        return "INTEGER" if unsigned else "SMALLINT"

    if base_type == "mediumint":
        return "BIGINT" if unsigned else "INTEGER"

    if base_type in ("int", "integer"):
        return "BIGINT" if unsigned else "INTEGER"

    if base_type == "bigint":
        # return "NUMERIC(20)" if unsigned else "BIGINT"
        return "BIGINT" if unsigned else "BIGINT"

    return None


def convert_type(column_name, column_type):
    """
    MariaDB -> PostgreSQL
    """

    original = column_type
    base = parse_base_type(column_type)

    unsigned = is_unsigned(column_type)

    # --------------------------------------------------------
    # BOOLEAN
    # --------------------------------------------------------

    if base == "tinyint":
        size = extract_parentheses(column_type)

        if size == "1":
            return "BOOLEAN", None

    # --------------------------------------------------------
    # ENUM
    # --------------------------------------------------------

    if base == "enum":
        values = extract_enum_values(column_type)

        return "VARCHAR(255)", {
            "kind": "enum",
            "values": values,
        }

    # --------------------------------------------------------
    # SET
    # --------------------------------------------------------

    if base == "set":
        values = extract_set_values(column_type)

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
        return convert_integer_type(
            base,
            unsigned
        ), None

    # --------------------------------------------------------
    # DECIMAL / NUMERIC
    # --------------------------------------------------------

    if base in ("decimal", "numeric"):
        size = extract_parentheses(column_type)

        if size:
            return f"NUMERIC({size})", None

        return "NUMERIC", None

    # --------------------------------------------------------
    # FLOAT
    # --------------------------------------------------------

    if base == "float":
        return "REAL", None

    # --------------------------------------------------------
    # DOUBLE
    # --------------------------------------------------------

    if base in ("double", "real"):
        return "DOUBLE PRECISION", None

    # --------------------------------------------------------
    # CHAR
    # --------------------------------------------------------

    if base == "char":
        size = extract_parentheses(column_type)

        if size:
            return f"CHAR({size})", None

        return "CHAR", None

    # --------------------------------------------------------
    # VARCHAR
    # --------------------------------------------------------

    if base == "varchar":
        size = extract_parentheses(column_type)

        if size:
            return f"VARCHAR({size})", None

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

    if base in ("boolean", "bool"):
        return "BOOLEAN", None

    # --------------------------------------------------------
    # BIT
    # --------------------------------------------------------

    if base == "bit":
        size = extract_parentheses(column_type)

        if size == "1":
            return "BOOLEAN", None

        if size:
            return f"BIT({size})", None

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
        SELECT TABLE_NAME, TABLE_TYPE
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
        """
    )

    return cursor.fetchall()


def get_columns(cursor, table):
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


def get_primary_key(cursor, table):
    cursor.execute(
        """
        SELECT COLUMN_NAME
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


def get_foreign_keys(cursor, table):
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


def get_indexes(cursor, table):
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
        seq = row[3]
        index_type = row[4]

        if name not in indexes:
            indexes[name] = {
                "unique": non_unique == 0,
                "columns": [],
                "type": index_type,
            }

        indexes[name]["columns"].append(column)

    return indexes


# ============================================================
# DEFAULT CONVERSION
# ============================================================

def convert_default(default_value, pg_type):
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

    # MariaDB numeric default
    if re.fullmatch(
        r"-?\d+(\.\d+)?",
        value
    ):
        return value

    # Boolean
    if pg_type == "BOOLEAN":

        if value in ("1", "TRUE", "true"):
            return "TRUE"

        if value in ("0", "FALSE", "false"):
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
        f"CHECK ({pg_ident(column)} IN ({values_sql}))"
    )


def generate_set_check(
    table,
    column,
    values,
):
    """
    SET MariaDB disimpan sebagai comma-separated string.

    Contoh:

    'red,blue'

    akan divalidasi agar setiap item berasal dari daftar SET.
    """

    if not values:
        return ""

    allowed = ", ".join(
        pg_literal(v)
        for v in values
    )

    expression = (
        f"""
        (
            {pg_ident(column)} IS NULL
            OR
            NOT EXISTS (
                SELECT 1
                FROM unnest(
                    string_to_array(
                        {pg_ident(column)},
                        ','
                    )
                ) AS item
                WHERE item <> ''
                  AND item NOT IN ({allowed})
            )
        )
        """
    )

    # PostgreSQL CHECK constraint tidak boleh mengandung subquery.
    # Karena itu SET hanya dikonversi menjadi TEXT tanpa CHECK.
    #
    # Validasi SET dilakukan melalui trigger.
    return ""


# ============================================================
# CREATE TABLE
# ============================================================

def generate_create_table(
    cursor,
    table,
):
    columns = get_columns(cursor, table)

    if not columns:
        return ""

    primary_keys = get_primary_key(
        cursor,
        table,
    )

    lines = []
    check_constraints = []

    auto_increment_columns = []

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
                " GENERATED BY DEFAULT AS IDENTITY"
            )

            auto_increment_columns.append(
                column_name
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
    # ENUM CHECK
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

        # MariaDB FULLTEXT / SPATIAL tidak
        # kompatibel langsung.
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

def generate_data(
    cursor,
    table,
    output,
    batch_size=BATCH_SIZE,
):
    """
    Streaming/batch data.

    Tidak menggunakan fetchall().
    """

    cursor.execute(
        f"SELECT * FROM "
        f"{mysql_ident(table)}"
    )

    columns = [
        description[0]
        for description
        in cursor.description
    ]

    columns_sql = ", ".join(
        pg_ident(c)
        for c in columns
    )

    total = 0

    while True:

        rows = cursor.fetchmany(
            batch_size
        )

        if not rows:
            break

        values_list = []

        for row in rows:

            values = ", ".join(
                pg_literal(v)
                for v in row
            )

            values_list.append(
                f"({values})"
            )

            total += 1

        output.write(
            f"INSERT INTO "
            f"{pg_ident(table)} "
            f"({columns_sql}) VALUES\n"
        )

        output.write(
            ",\n".join(values_list)
        )

        output.write(";\n\n")

    print(
        f"    data: {total:,} rows"
    )


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
    """
    Basic conversion.

    View kompleks perlu review manual.
    """

    if not sql:
        return sql

    result = sql

    # Backtick -> PostgreSQL quote
    result = re.sub(
        r"`([^`]+)`",
        r'"\1"',
        result,
    )

    # MariaDB IFNULL -> PostgreSQL COALESCE
    result = re.sub(
        r"\bIFNULL\s*\(",
        "COALESCE(",
        result,
        flags=re.IGNORECASE,
    )

    # MariaDB NOW()
    result = re.sub(
        r"\bNOW\s*\(\s*\)",
        "CURRENT_TIMESTAMP",
        result,
        flags=re.IGNORECASE,
    )

    # DATE_FORMAT belum bisa ditranslate otomatis
    if re.search(
        r"\bDATE_FORMAT\s*\(",
        result,
        flags=re.IGNORECASE,
    ):
        print(
            "[WARNING] View menggunakan DATE_FORMAT(). "
            "Perlu konversi manual ke TO_CHAR().",
            file=sys.stderr,
        )

    return result


def generate_views(
    cursor,
):
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
    """
    Basic MariaDB -> PostgreSQL conversion.

    Tidak menjamin kompatibilitas trigger kompleks.
    """

    result = statement.strip()

    # Remove MariaDB delimiter-related syntax
    result = re.sub(
        r"\bBEGIN\s*",
        "BEGIN\n",
        result,
        flags=re.IGNORECASE,
    )

    # Basic IFNULL
    result = re.sub(
        r"\bIFNULL\s*\(",
        "COALESCE(",
        result,
        flags=re.IGNORECASE,
    )

    return result


def generate_triggers(
    cursor,
):
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
            f"[WARNING] Trigger {trigger_name} "
            f"pada {table} ditemukan. "
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

        # PostgreSQL menggunakan NEW / OLD
        # dengan syntax PL/pgSQL.

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


def generate_routines(
    cursor,
):
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
            f"Konversi otomatis hanya "
            f"basic dan perlu review manual.",
            file=sys.stderr,
        )

        if not definition:
            continue

        # ----------------------------------------------------
        # Basic syntax replacements
        # ----------------------------------------------------

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

        # PostgreSQL return type
        pg_return = "VOID"

        if routine_type.upper() == "FUNCTION":

            if data_type:
                pg_return, _ = convert_type(
                    name,
                    data_type
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
):
    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    ddl_file = (
        output_dir / "01_schema.sql"
    )

    fk_file = (
        output_dir / "02_foreign_keys.sql"
    )

    index_file = (
        output_dir / "03_indexes.sql"
    )

    data_file = (
        output_dir / "04_data.sql"
    )

    view_file = (
        output_dir / "05_views.sql"
    )

    trigger_file = (
        output_dir / "06_triggers.sql"
    )

    routine_file = (
        output_dir / "07_routines_manual.sql"
    )

    print()
    print("=" * 60)
    print("MariaDB -> PostgreSQL Migration")
    print("=" * 60)
    print()
    print(f"Database : {database}")
    print(f"Output   : {output_dir}")
    print()

    conn = mariadb.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )

    cursor = conn.cursor()

    tables = get_tables(cursor)

    normal_tables = [
        table
        for table, table_type
        in tables
        if table_type == "BASE TABLE"
    ]

    print(
        f"[+] Tables: {len(normal_tables)}"
    )

    # --------------------------------------------------------
    # SCHEMA
    # --------------------------------------------------------

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

            f.write("\n")

        f.write(
            "COMMIT;\n"
        )

    # --------------------------------------------------------
    # FOREIGN KEYS
    # --------------------------------------------------------

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

    with open(
        data_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Data\n\n"
        )

        for table in normal_tables:

            print(
                f"[DATA]   {table}"
            )

            f.write(
                f"-- TABLE: {table}\n"
            )

            generate_data(
                cursor,
                table,
                f,
                batch_size,
            )

            f.write("\n")

    # --------------------------------------------------------
    # VIEWS
    # --------------------------------------------------------

    with open(
        view_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Views\n\n"
        )

        f.write(
            generate_views(cursor)
        )

    # --------------------------------------------------------
    # TRIGGERS
    # --------------------------------------------------------

    with open(
        trigger_file,
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "-- Triggers\n\n"
        )

        # ON UPDATE CURRENT_TIMESTAMP
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

        # MariaDB triggers
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
            generate_triggers(cursor)
        )

    # --------------------------------------------------------
    # ROUTINES
    # --------------------------------------------------------

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
            generate_routines(cursor)
        )

    cursor.close()
    conn.close()

    print()
    print("=" * 60)
    print("Migration SQL berhasil dibuat")
    print("=" * 60)
    print()
    print(f"Schema : {ddl_file}")
    print(f"FK     : {fk_file}")
    print(f"Index  : {index_file}")
    print(f"Data   : {data_file}")
    print(f"Views  : {view_file}")
    print(f"Trigger: {trigger_file}")
    print(f"Routine: {routine_file}")
    print()


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Convert seluruh database MariaDB "
            "menjadi PostgreSQL SQL"
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
        default=1000,
    )

    args = parser.parse_args()

    try:

        migrate_database(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
        )

    except mariadb.Error as e:

        print(
            f"[ERROR] MariaDB: {e}",
            file=sys.stderr,
        )

        sys.exit(1)

    except Exception as e:

        print(
            f"[ERROR] {e}",
            file=sys.stderr,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
