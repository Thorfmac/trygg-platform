# =============================================================
# scripts/backup.ps1
# Nightly PostgreSQL backup to Azure Blob Storage
# Runs via Windows Task Scheduler at 2am
# =============================================================

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = "C:\Temp\trygg_backup_$timestamp.sql"
$containerName = "postgres-backups"
$blobName = "trygg_backup_$timestamp.sql"

# Load connection string from .env
$envPath = "C:\Users\thorfinnmaciver\trygg-platform\.env"
$envContent = Get-Content $envPath
$connectionString = ($envContent | Select-String "AZURE_STORAGE_CONNECTION_STRING=").ToString().Split("=", 2)[1]
$dbPassword = ($envContent | Select-String "DB_PASSWORD=").ToString().Split("=", 2)[1]

# Create temp dir if needed
New-Item -ItemType Directory -Force -Path C:\Temp | Out-Null

# Dump the database from the Docker container
$env:PGPASSWORD = $dbPassword
docker exec trygg-db pg_dump -U trygg trygg > $backupFile

# Upload to Azure Blob Storage
az storage blob upload `
    --connection-string $connectionString `
    --container-name $containerName `
    --name $blobName `
    --file $backupFile `
    --overwrite

# Clean up local file
Remove-Item $backupFile

Write-Host "Backup complete: $blobName"