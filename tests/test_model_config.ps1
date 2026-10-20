# Offline config and CLI boundary tests; run with pwsh -NoProfile -File tests/test_model_config.ps1.
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$source = Join-Path $PSScriptRoot '../openclaw.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
# Load function definitions without invoking the installer or interactive main loop.
$ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }

function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
function UI-Info { }
function UI-OK { }
function UI-Warn { }
function UI-Err { throw 'Unexpected error path' }
function Ensure-Fzf { }
function Start-Gateway { }
function Press-Enter { }
function Show-Menu { param($Header, $Options) return $Options[0] }
function Get-Command { return $null }
function cmd { $script:selected = $args }
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec, $ErrorAction)
    Assert ($Uri -eq "$script:base/models") 'Incorrect discovery URL'
    Assert ($Headers.Authorization -eq 'Bearer fixture-key') 'Incorrect discovery authorization'
    $script:requests++
    return [PSCustomObject]@{ data = @('MiniMax-M2.7', 'MiniMax-M2.5', 'custom-model', 'MiniMax-M3', 'MiniMax-M3.1-Flash', 'speech-2.8-hd') | ForEach-Object { [PSCustomObject]@{ id = $_ } } }
}
$workRoot = if ($env:TEST_WORKDIR) { $env:TEST_WORKDIR } else { $PSScriptRoot }
$script:OpenClawJson = Join-Path $workRoot 'powershell-config.json'
$script:requests = 0
foreach ($script:base in @('https://api.minimax.io/v1', 'https://api.minimaxi.com/v1', 'https://gateway.example/v1')) {
    '{}' | Set-Content $script:OpenClawJson
    Assert (Add-ModelsFromProvider direct $script:base fixture-key) 'Import failed'
    $json = Get-Content $script:OpenClawJson -Raw | ConvertFrom-Json
    $models = $json.models.providers.direct.models
    Assert (($models.id -join ',') -eq 'MiniMax-M3.1-Flash,custom-model,MiniMax-M3,speech-2.8-hd') 'Incorrect ordinary import choices'
    Assert ($json.models.providers.direct.api -eq 'openai-completions') 'Incorrect API contract'
    if ($script:base -notmatch 'gateway') {
        Assert ($models[0].contextWindow -eq 1000000 -and $models[0].maxTokens -eq 524288) 'Incorrect Flash limits'
        Assert (($models[0].input -join ',') -eq 'text,image,video') 'Multimodal input lost'
        Assert ($models[0].cost.input -eq .15 -and $models[0].cost.cacheRead -eq .015) 'Incorrect Flash pricing'
        Assert ($null -eq $models[0].cost.cacheWrite) 'Unknown cache write price invented'
        Assert ($models[2].cost.input -eq .3) 'Incorrect M3 base tier'
    } else {
        Assert ($models[0].contextWindow -eq 1048576) 'Direct metadata applied to a gateway'
    }
}
$script:base = 'https://api.minimax.io/v1'
@{
    models = @{ providers = @{ direct = @{
        baseUrl = $script:base; apiKey = 'fixture-key'; api = 'openai-completions'
        models = @(@{ id = 'MiniMax-M2.7'; maxTokens = 42 }, @{ id = 'private-alias'; maxTokens = 99 })
    } } }
    agents = @{ defaults = @{ model = @{ primary = 'direct/MiniMax-M2.7' }; models = @{ 'direct/MiniMax-M2.7' = @{ params = @{ temperature = .7 } } } } }
} | ConvertTo-Json -Depth 20 | Set-Content $script:OpenClawJson
$savedConfig = Get-Content $script:OpenClawJson -Raw
Assert (Sync-OpenClawAPIModels) 'Sync failed'
$json = Get-Content $script:OpenClawJson -Raw | ConvertFrom-Json
Assert ($json.agents.defaults.model.primary -eq 'direct/MiniMax-M2.7') 'Sync reset a saved model'
Assert (($json.models.providers.direct.models | Where-Object id -eq 'MiniMax-M2.7').maxTokens -eq 42) 'Saved metadata changed'
Assert ($json.models.providers.direct.models.id -contains 'private-alias') 'Custom alias lost'
Assert ($json.models.providers.direct.models.id -notcontains 'MiniMax-M2.5') 'Legacy discovery reintroduced'
Assert (($json.models.providers.direct.models | Where-Object id -eq 'MiniMax-M3.1-Flash').maxTokens -eq 524288) 'Sync inherited wrong metadata'
$savedConfig | Set-Content $script:OpenClawJson
Assert (Add-ModelsFromProvider direct $script:base fixture-key) 'Reimport failed'
$json = Get-Content $script:OpenClawJson -Raw | ConvertFrom-Json
Assert ($json.models.providers.direct.models.id -contains 'private-alias') 'Reimport lost custom alias'
Assert (($json.models.providers.direct.models | Where-Object id -eq 'MiniMax-M2.7').maxTokens -eq 42) 'Reimport changed saved metadata'
Assert ($json.agents.defaults.model.primary -eq 'direct/MiniMax-M2.7') 'Reimport reset primary'
$script:first = $null
Change-Model
Assert (($script:selected -join ' ') -eq '/c openclaw models set direct/MiniMax-M3.1-Flash') 'Picker did not send Flash to CLI'
Assert ($script:requests -eq 5) 'Unexpected discovery request count'
Write-Output 'PASS: PowerShell parser, importer, sync, saved/custom preservation, regions, gateway metadata and picker CLI boundary'
