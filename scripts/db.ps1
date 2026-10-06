<#
.SYNOPSIS
  Database setup for the Job Tracker on an existing PostgreSQL service (15 or newer; developed against 18).

.DESCRIPTION
  Two commands, deliberately separate:

    bootstrap   ONE-TIME, run by you. Needs the PostgreSQL admin ("postgres") password, which is typed at a prompt
                and kept only in memory. Creates the app role (CREATEDB, owns the database), a read-only role for
                DBeaver, the database, and the read-only permissions. Safe to run again: existing roles and the
                database are kept and the passwords are updated.

    apply       Routine, runs as the APP role (password from DATABASE_URL in .env). Loads db\schema.sql if the
                tables do not exist yet, then loads the technology tags. Never needs the admin password.

  Connection details (host, port, database, app role, app password) come from DATABASE_URL in the project's .env.
  Passwords are never put on a command line or written to disk: they travel to psql through environment variables
  that exist only for the lifetime of this script.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\db.ps1 bootstrap -DryRun     # show what it would do, change nothing
  powershell -ExecutionPolicy Bypass -File scripts\db.ps1 bootstrap
  powershell -ExecutionPolicy Bypass -File scripts\db.ps1 apply

.NOTES
  Requires psql 15+ (uses \getenv). Looks for psql.exe on PATH, then in C:\Program Files\PostgreSQL\<newest>\bin,
  or use $env:PSQL_PATH. apply also needs uv (for the tag seeding step).
  This file is intentionally plain ASCII: Windows PowerShell 5.1 mangles non-ASCII characters in scripts.
#>
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('bootstrap', 'apply')]
  [string]$Command,

  [string]$AdminUser = 'postgres',   # bootstrap: the superuser to connect as
  [string]$ReadOnlyRole = '',        # bootstrap: defaults to "<database>_ro"
  [switch]$RestrictPublic,           # bootstrap: also stop other roles from connecting to the database
  [switch]$DryRun                    # bootstrap: print the plan and the SQL, change nothing
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$BoundParams = $PSBoundParameters   # captured here: inside a function, $PSBoundParameters would be the function's own

# ----------------------------------------------------------------------------------------------- helpers
function Find-Psql {
  if ($env:PSQL_PATH -and (Test-Path -LiteralPath $env:PSQL_PATH)) { return $env:PSQL_PATH }
  $cmd = Get-Command psql.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $found = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d+$' } |
    Sort-Object { [int]$_.Name } -Descending |
    ForEach-Object { Join-Path $_.FullName 'bin\psql.exe' } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
  if ($found) { return $found }
  throw 'psql.exe not found. Install the PostgreSQL client tools, or set $env:PSQL_PATH to the full path of psql.exe.'
}

function Read-TextFile([string]$Path) {
  # Returns @{ Text; Utf16 }. Detects a byte-order mark so a file saved as UTF-16 (Windows PowerShell's default for
  # ">" and Out-File, and Notepad's "Unicode" option) is read correctly instead of looking empty.
  $b = [IO.File]::ReadAllBytes($Path)
  if ($b.Length -ge 2 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE) { return @{ Text = [Text.Encoding]::Unicode.GetString($b, 2, $b.Length - 2); Utf16 = $true } }
  if ($b.Length -ge 2 -and $b[0] -eq 0xFE -and $b[1] -eq 0xFF) { return @{ Text = [Text.Encoding]::BigEndianUnicode.GetString($b, 2, $b.Length - 2); Utf16 = $true } }
  if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) { return @{ Text = [Text.Encoding]::UTF8.GetString($b, 3, $b.Length - 3); Utf16 = $false } }
  return @{ Text = [Text.Encoding]::UTF8.GetString($b); Utf16 = $false }
}

function Get-DbConfig {
  # Reads DATABASE_URL from .env. Accepts postgresql://user:password@host:port/db (percent-encode special characters
  # in the password) or the key-value form: host=... port=... dbname=... user=... password=...
  # $cfg.Problem explains, in one sentence, why no usable password was found (empty when everything is fine).
  $cfg = @{ Host = 'localhost'; Port = '5432'; Db = 'jobtracker'; User = 'jobtracker'; Password = ''; Problem = '' }
  $envFile = Join-Path $Root '.env'
  $raw = $null
  if (-not (Test-Path -LiteralPath $envFile)) {
    if (Test-Path -LiteralPath (Join-Path $Root '.env.txt')) {
      $cfg.Problem = "There is no .env but there is a .env.txt (Notepad adds .txt). Rename it to .env in $Root."
    } else {
      $cfg.Problem = "There is no .env file in $Root. Copy .env.example to .env and edit it."
    }
    return $cfg
  }
  $file = Read-TextFile $envFile
  $lines = $file.Text -split "\r?\n"
  foreach ($line in $lines) {
    if ($line -match '^\s*DATABASE_URL\s*=\s*(.*?)\s*$') { $raw = $Matches[1].Trim('"').Trim("'") }
  }
  if ($file.Utf16) {
    $cfg.Problem = '.env is saved as UTF-16, which the app cannot read. Re-save it as UTF-8 (in VS Code: click the encoding at the bottom right, Save with Encoding, UTF-8).'
    return $cfg
  }
  if (-not $raw) {
    if ($lines | Where-Object { $_ -match '^\s*#\s*DATABASE_URL\s*=' }) {
      $cfg.Problem = 'The DATABASE_URL line in .env is commented out (it starts with #). Remove the # and put your password in it.'
    } elseif ($lines | Where-Object { $_ -match 'DATABASE_URL' }) {
      $cfg.Problem = 'DATABASE_URL in .env is not on a line of its own, or its value is empty. It must be a line starting with DATABASE_URL= .'
    } else {
      $cfg.Problem = 'There is no DATABASE_URL line in .env.'
    }
    return $cfg
  }

  if ($raw -match '^postgres(ql)?://') {
    $m = [regex]::Match($raw, '^postgres(?:ql)?://(?:(?<user>[^:@/]*)(?::(?<pw>[^@]*))?@)?(?<host>[^:/?#]*)(?::(?<port>\d+))?(?:/(?<db>[^?#]*))?')
    if ($m.Groups['user'].Value) { $cfg.User = [uri]::UnescapeDataString($m.Groups['user'].Value) }
    if ($m.Groups['pw'].Value)   { $cfg.Password = [uri]::UnescapeDataString($m.Groups['pw'].Value) }
    if ($m.Groups['host'].Value) { $cfg.Host = $m.Groups['host'].Value }
    if ($m.Groups['port'].Value) { $cfg.Port = $m.Groups['port'].Value }
    if ($m.Groups['db'].Value)   { $cfg.Db = [uri]::UnescapeDataString($m.Groups['db'].Value) }
  } else {
    foreach ($kv in [regex]::Matches($raw, "(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")) {
      $v = $kv.Groups[2].Value.Trim("'")
      switch ($kv.Groups[1].Value) {
        'host'     { $cfg.Host = $v }
        'port'     { $cfg.Port = $v }
        'dbname'   { $cfg.Db = $v }
        'user'     { $cfg.User = $v }
        'password' { $cfg.Password = $v }
      }
    }
  }
  if (-not $cfg.Password) {
    $cfg.Problem = 'DATABASE_URL in .env has no password. Expected postgresql://USER:PASSWORD@HOST:PORT/DATABASE (or the key-value form with password=...).'
  }
  return $cfg
}

function Test-Identifier([string]$Name, [string]$What) {
  if ($Name -notmatch '^[a-z_][a-z0-9_]{0,62}$') {
    throw "$What '$Name' must be lowercase letters, digits and underscores only (it is used as a PostgreSQL identifier)."
  }
}

function Read-Secret([string]$Prompt) {
  $s = Read-Host -AsSecureString $Prompt
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

function Set-ProcessEnv([string]$Name, [string]$Value) {
  [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Invoke-Psql {
  param([string]$User, [string]$Database, [string[]]$ExtraArgs = @(), [string]$InputSql = $null)
  $psqlArgs = @('-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', $script:Cfg.Host, '-p', $script:Cfg.Port,
                '-U', $User, '-d', $Database, '-w') + $ExtraArgs   # -w: never prompt for a password
  if ($null -ne $InputSql) { $InputSql | & $script:Psql @psqlArgs } else { & $script:Psql @psqlArgs }
  if ($LASTEXITCODE -ne 0) { throw "psql exited with code $LASTEXITCODE" }
}

# ------------------------------------------------------------------------------------------------ setup
$Cfg = Get-DbConfig
$AppRole = $Cfg.User
$Database = $Cfg.Db
if (-not $ReadOnlyRole) { $ReadOnlyRole = "${Database}_ro" }
Test-Identifier $AppRole 'App role (user in DATABASE_URL)'
Test-Identifier $Database 'Database name'
Test-Identifier $ReadOnlyRole 'Read-only role'
$Psql = Find-Psql

# ------------------------------------------------------------------------------------------------ bootstrap
$BootstrapRoles = @'
\getenv app_pw APP_PW
\getenv ro_pw RO_PW
SELECT format('CREATE ROLE %I LOGIN CREATEDB PASSWORD %L', :'app_role', :'app_pw')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
SELECT format('ALTER ROLE %I LOGIN CREATEDB PASSWORD %L', :'app_role', :'app_pw') \gexec
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB PASSWORD %L', :'ro_role', :'ro_pw')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_role') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB PASSWORD %L', :'ro_role', :'ro_pw') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'dbname', :'app_role')
  WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'dbname', :'app_role') \gexec
'@

$BootstrapGrants = @'
GRANT CONNECT ON DATABASE :"dbname" TO :"ro_role";
GRANT USAGE ON SCHEMA public TO :"ro_role";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"app_role" IN SCHEMA public GRANT SELECT ON TABLES TO :"ro_role";
-- ALL TABLES does not cover sequences, and pg_dump (and DBeaver's sequence view) must read them
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO :"ro_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"app_role" IN SCHEMA public GRANT SELECT ON SEQUENCES TO :"ro_role";
'@

$RestrictSql = 'REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;'

$VerifySql = @'
SELECT rolname AS role, rolcanlogin AS can_login, rolcreatedb AS can_create_db, rolsuper AS superuser
  FROM pg_roles WHERE rolname IN (:'app_role', :'ro_role') ORDER BY rolname;
SELECT datname AS database, pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname = :'dbname';
'@

function Invoke-Bootstrap {
  if ($AppRole -eq $AdminUser) { throw "The app role in DATABASE_URL ('$AppRole') is the admin role. Use a separate, non-superuser app role." }
  if ($ReadOnlyRole -eq $AppRole) { throw 'The read-only role must differ from the app role.' }

  $roleVars = @('-v', "app_role=$AppRole", '-v', "ro_role=$ReadOnlyRole", '-v', "dbname=$Database")

  if ($DryRun) {
    Write-Host "DRY RUN - nothing will be changed and no password is requested."
    Write-Host "Server       : $($Cfg.Host):$($Cfg.Port)  (psql: $Psql)"
    Write-Host "Connect as   : $AdminUser (you will be asked for its password)"
    Write-Host "App role     : $AppRole  (LOGIN, CREATEDB; password = the one in DATABASE_URL in .env; owns '$Database')"
    Write-Host "Read-only    : $ReadOnlyRole  (LOGIN, SELECT only; you will be asked for a new password)"
    Write-Host "Database     : $Database"
    Write-Host "Restrict PUBLIC connect: $($RestrictPublic.IsPresent)"
    Write-Host "`n--- 1. on database 'postgres' ---`n$BootstrapRoles"
    Write-Host "`n--- 2. on database '$Database' ---`n$BootstrapGrants"
    if ($RestrictPublic) { Write-Host $RestrictSql }
    Write-Host "`n(:'name' and :`"name`" are psql variables; passwords are read from environment variables, never printed.)"
    return
  }

  if (-not $Cfg.Password) {
    throw "$($Cfg.Problem) Example line: DATABASE_URL=postgresql://$AppRole`:YOUR_PASSWORD@$($Cfg.Host):$($Cfg.Port)/$Database"
  }
  $adminPw = Read-Secret "Password for PostgreSQL admin role '$AdminUser'"
  if (-not $adminPw) { throw 'No admin password entered.' }
  $roPw = Read-Secret "NEW password for read-only role '$ReadOnlyRole' (used only in DBeaver, not stored anywhere)"
  $roPw2 = Read-Secret 'Repeat it'
  if (-not $roPw -or $roPw -ne $roPw2) { throw 'The two read-only passwords are empty or do not match.' }

  try {
    Set-ProcessEnv 'PGPASSWORD' $adminPw
    Set-ProcessEnv 'APP_PW' $Cfg.Password
    Set-ProcessEnv 'RO_PW' $roPw
    Set-ProcessEnv 'PGCLIENTENCODING' 'UTF8'

    Write-Host "Checking required extensions are installed on the server..."
    $missing = Invoke-Psql -User $AdminUser -Database 'postgres' -ExtraArgs @('-t', '-A', '-c',
      "SELECT string_agg(e, ', ') FROM unnest(ARRAY['pg_trgm','pgcrypto']) e WHERE e NOT IN (SELECT name FROM pg_available_extensions)")
    if (($missing | Out-String).Trim()) {
      throw "These extensions are not available on the server: $(($missing | Out-String).Trim()). Install the PostgreSQL contrib package, then run bootstrap again."
    }

    Write-Host "Creating roles and database (existing ones are kept)..."
    Invoke-Psql -User $AdminUser -Database 'postgres' -ExtraArgs $roleVars -InputSql $BootstrapRoles

    Write-Host "Granting read-only access in '$Database'..."
    $grants = $BootstrapGrants + $(if ($RestrictPublic) { "`n$RestrictSql" } else { '' }) + "`n" + $VerifySql
    Invoke-Psql -User $AdminUser -Database $Database -ExtraArgs $roleVars -InputSql $grants

    Write-Host "`nDone. Next: powershell -ExecutionPolicy Bypass -File scripts\db.ps1 apply"
  }
  finally {
    foreach ($n in 'PGPASSWORD', 'APP_PW', 'RO_PW', 'PGCLIENTENCODING') { Set-ProcessEnv $n $null }
  }
}

# ------------------------------------------------------------------------------------------------ apply
function Invoke-Apply {
  foreach ($opt in 'DryRun', 'RestrictPublic', 'AdminUser', 'ReadOnlyRole') {
    if ($BoundParams.ContainsKey($opt)) {
      Write-Warning "Option -$opt applies to bootstrap only; ignored by apply."
    }
  }
  if (-not $Cfg.Password) {
    throw "$($Cfg.Problem) See .env.example, then run apply again."
  }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv was not found on PATH (needed to load the technology tags). Open a new terminal, or install uv.'
  }

  try {
    Set-ProcessEnv 'PGPASSWORD' $Cfg.Password
    Set-ProcessEnv 'PGCLIENTENCODING' 'UTF8'

    Write-Host "Connecting to $Database on $($Cfg.Host):$($Cfg.Port) as '$AppRole'..."
    try {
      Invoke-Psql -User $AppRole -Database $Database -ExtraArgs @('-t', '-A', '-c', 'SELECT 1') | Out-Null  # a reachable database and a working login
    } catch {
      throw "Could not connect as '$AppRole' to '$Database'. If the role and database do not exist yet, run 'db.ps1 bootstrap' first. Otherwise check DATABASE_URL in .env. ($($_.Exception.Message))"
    }

    # One entry point for schema changes: the baseline on an empty database, then pending db\migrations\*.sql, each
    # recorded once (src/jobtracker/migrate.py). Back the database up before the first run after an upgrade.
    Write-Host "Migrating..."
    Push-Location $Root
    try {
      & uv run python -m jobtracker migrate
      if ($LASTEXITCODE -ne 0) { throw 'migration failed' }
    } finally { Pop-Location }

    Write-Host "Loading technology tags..."
    Push-Location $Root
    try {
      & uv run python -m jobtracker seed
      if ($LASTEXITCODE -ne 0) { throw 'tag seeding failed' }
    } finally { Pop-Location }

    Write-Host "`nResult:"
    Invoke-Psql -User $AppRole -Database $Database -ExtraArgs @('-c',
      "SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE') AS tables, (SELECT count(*) FROM information_schema.views WHERE table_schema='public') AS views, (SELECT count(*) FROM technologies) AS technologies, (SELECT count(*) FROM jobs) AS jobs")
  }
  finally {
    foreach ($n in 'PGPASSWORD', 'PGCLIENTENCODING') { Set-ProcessEnv $n $null }
  }
}

switch ($Command) {
  'bootstrap' { Invoke-Bootstrap }
  'apply'     { Invoke-Apply }
}
