param(
  [int]$Year = 2024,
  [string]$Scope = "source_universe",
  [string]$Config = "config/sources.yaml"
)

python -m crime_index.cli run-all --year $Year --scope $Scope --config $Config
