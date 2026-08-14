# Nod's voice worker.
#
# Started once and kept alive; reads one line of text per utterance from stdin
# and speaks it. Two reasons it is a script file rather than a -Command string:
#
#   `powershell -Command -` drains stdin to EOF before executing anything, so
#   it can never answer line by line. A script with its own read loop can.
#
#   More importantly, the text arrives as *data* on stdin and is only ever
#   bound to a variable. Nothing spoken is ever parsed as PowerShell, so a
#   meeting title or transcript containing $(...), backticks or a semicolon is
#   read aloud rather than run. Building a -Command string would have made
#   every utterance an injection site, and the things Nod says are assembled
#   from calendar entries and speech recognition -- neither of which is trusted.

param(
    [string]$VoiceHint = "Zira",
    [int]$Rate = 1
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $match = $synth.GetInstalledVoices() |
        Where-Object { $_.VoiceInfo.Name -like "*$VoiceHint*" } |
        Select-Object -First 1
    if ($match) { $synth.SelectVoice($match.VoiceInfo.Name) }
} catch { }

$synth.Rate = $Rate
$synth.SetOutputToDefaultAudioDevice()

Write-Output "READY"

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }          # stdin closed
    if ($line -eq "__NOD_EXIT__") { break }
    if ($line.Trim().Length -eq 0) { continue }
    try { $synth.Speak($line) } catch { }
    # Tell the parent this utterance finished, so it can tell speaking from
    # merely queued -- the mic listener uses that to avoid hearing Nod itself.
    Write-Output "DONE"
}
