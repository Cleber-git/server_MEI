param(
  [Parameter(Mandatory = $true)]
  [string]$InputWorkbook,

  [Parameter(Mandatory = $true)]
  [string]$OutputFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

$workbookPath = (Resolve-Path -LiteralPath $InputWorkbook).Path
$archive = [System.IO.Compression.ZipFile]::OpenRead($workbookPath)

function Read-ZipEntry {
  param([string]$Name)

  $entry = $archive.Entries | Where-Object FullName -eq $Name | Select-Object -First 1
  if (-not $entry) {
    return $null
  }

  $reader = [System.IO.StreamReader]::new($entry.Open())
  try {
    return $reader.ReadToEnd()
  }
  finally {
    $reader.Dispose()
  }
}

try {
  $sharedStrings = @()
  $sharedStringsXml = Read-ZipEntry "xl/sharedStrings.xml"
  if ($sharedStringsXml) {
    $sharedStringsDocument = [xml]$sharedStringsXml
    foreach ($item in $sharedStringsDocument.sst.si) {
      $sharedStrings += (($item.SelectNodes('.//*[local-name()="t"]') | ForEach-Object InnerText) -join "")
    }
  }

  $workbookDocument = [xml](Read-ZipEntry "xl/workbook.xml")
  $relationsDocument = [xml](Read-ZipEntry "xl/_rels/workbook.xml.rels")
  $firstSheet = $workbookDocument.workbook.sheets.sheet | Select-Object -First 1
  if (-not $firstSheet) {
    throw "A planilha não possui abas."
  }

  $relationshipId = $firstSheet.GetAttribute(
    "id",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  )
  $relationship = $relationsDocument.Relationships.Relationship |
    Where-Object Id -eq $relationshipId |
    Select-Object -First 1
  if (-not $relationship) {
    throw "Não foi possível localizar os dados da primeira aba."
  }

  $sheetPath = $relationship.Target.TrimStart("/")
  if (-not $sheetPath.StartsWith("xl/")) {
    $sheetPath = "xl/$sheetPath"
  }

  $sheetDocument = [xml](Read-ZipEntry $sheetPath)

  function Get-CellText {
    param($Cell)

    if (-not $Cell) {
      return ""
    }

    $cellType = $Cell.GetAttribute("t")
    $valueNode = $Cell.SelectSingleNode('./*[local-name()="v"]')
    $cellValue = if ($valueNode) { [string]$valueNode.InnerText } else { "" }

    if ($cellType -eq "s" -and $cellValue) {
      return [string]$sharedStrings[[int]$cellValue]
    }

    if ($cellType -eq "inlineStr") {
      return [string](($Cell.SelectNodes('.//*[local-name()="t"]') | ForEach-Object InnerText) -join "")
    }

    return $cellValue
  }

  $headerRow = $sheetDocument.worksheet.sheetData.row | Select-Object -First 1
  $expectedHeaders = @("CIDADE", "UF", "PRAZO", "PRA", "FILIAL", "PRA", "PRA", "IBGE", "CEP INICIAL", "CEP FINAL")
  $actualHeaders = @()
  foreach ($cell in $headerRow.c) {
    $actualHeaders += (Get-CellText $cell).Trim()
  }

  for ($index = 0; $index -lt $expectedHeaders.Count; $index++) {
    if (-not $actualHeaders[$index].StartsWith($expectedHeaders[$index], [StringComparison]::OrdinalIgnoreCase)) {
      throw "Cabeçalho inesperado na coluna $($index + 1): '$($actualHeaders[$index])'."
    }
  }

  $areas = [System.Collections.Generic.List[object]]::new()
  foreach ($row in ($sheetDocument.worksheet.sheetData.row | Select-Object -Skip 1)) {
    $cells = @{}
    foreach ($cell in $row.c) {
      $column = ([regex]::Match([string]$cell.r, "^[A-Z]+")).Value
      $cells[$column] = (Get-CellText $cell).Trim()
    }

    if (-not $cells["A"] -or -not $cells["B"]) {
      continue
    }

    $areas.Add([ordered]@{
      city = $cells["A"]
      uf = $cells["B"]
      prazo = $cells["C"]
      praca = $cells["D"]
      filial = $cells["E"]
      pracaComercial = $cells["F"]
      regiao = $cells["G"]
      ibge = $cells["H"]
      cepInicial = $cells["I"]
      cepFinal = $cells["J"]
    })
  }

  if ($areas.Count -eq 0) {
    throw "Nenhuma cidade válida foi encontrada na planilha."
  }

  $json = ConvertTo-Json -InputObject $areas -Depth 3 -Compress
  $javascript = "window.KARAVAGGIO_PICKUP_AREAS = $json;`n"
  $outputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputFile))
  [System.IO.File]::WriteAllText($outputPath, $javascript, [System.Text.UTF8Encoding]::new($false))

  Write-Output "Geradas $($areas.Count) cidades de coleta em $outputPath"
}
finally {
  $archive.Dispose()
}
