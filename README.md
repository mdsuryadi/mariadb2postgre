## mariadb2postgre

>Install Python Environtment
```
apt install python3-venv
```

> Create Environtment
```
python3 -m venv .venv
ls -alh
source .venv/bin/activate
```
> Install Package
```
pip install mariadb
```

>Backup Mariadb Object
```
python3 mariadb2postgre.py --host IP --user USR  --password PWD  --database eems --output-dir ./postgresql_dump_eems
```

>Copy Stucture and Data into Postgresql
```
psql -h IP -U postgres -d postgres -c "SET search_path = eems;" -f path/to/file.sql
```
