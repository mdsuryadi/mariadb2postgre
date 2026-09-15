#!/bin/bash

set -e

DB_HOST="localhost"
DB_PORT="5432"
DB_USER="postgres"
DB_NAME="your_database"

BASE_DIR="./postgresql_dump_useradmin"

echo "======================================"
echo "PostgreSQL Import"
echo "======================================"

echo "[1] Schema"
psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$BASE_DIR/01_schema.sql"


echo "[2] Data"

find "$BASE_DIR/04_data" \
    -type f \
    -name "*.sql" \
    -print0 |
    sort -z |
while IFS= read -r -d '' file; do

    echo "Importing: $file"

    psql \
        -h "$DB_HOST" \
        -p "$DB_PORT" \
        -U "$DB_USER" \
        -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 \
        -c "SET search_path = eems;" \
        -f "$file"

done


echo "[3] Foreign Keys"
psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$BASE_DIR/02_foreign_keys.sql"


echo "[4] Indexes"
psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$BASE_DIR/03_indexes.sql"


echo "[5] Views"
psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$BASE_DIR/05_views.sql"


echo "[6] Triggers"
psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 \
    -f "$BASE_DIR/06_triggers.sql"


echo "======================================"
echo "Import completed"
echo "======================================"
