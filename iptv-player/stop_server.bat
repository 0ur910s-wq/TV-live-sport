@echo off
REM =====================================================================
REM  Σταματάει με ασφάλεια ΜΟΝΟ τη διεργασία που τρέχει το server.py —
REM  όχι όλες τις python.exe/pythonw.exe (π.χ. αν τρέχεις και άλλα δικά
REM  σου Python προγράμματα ταυτόχρονα, αυτά ΔΕΝ θα πειραχτούν).
REM =====================================================================

echo Αναζήτηση της διεργασίας server.py...

powershell -NoProfile -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*server.py*' }; ^
   if ($procs) { ^
       $procs | ForEach-Object { Write-Host ('Σταμάτημα PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }; ^
       Write-Host 'Ο server σταμάτησε.' ^
   } else { ^
       Write-Host 'Δεν βρέθηκε διεργασία server.py να τρέχει.' ^
   }"

pause
