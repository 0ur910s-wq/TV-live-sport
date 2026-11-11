@echo off
REM =====================================================================
REM  Εκκινητής του IPTV Player Pro server.
REM  Τρέχει το server.py ΧΩΡΙΣ να ανοίγει μαύρο παράθυρο console, ώστε να
REM  μπορεί να χρησιμοποιηθεί σε αυτόματη εκκίνηση (Task Scheduler) χωρίς
REM  να ενοχλεί.
REM
REM  ΠΡΟΣΟΧΗ: βάλε αυτό το .bat ΜΕΣΑ στον ίδιο φάκελο όπου βρίσκεται το
REM  server.py (μαζί με τα channels.json, settings.json κ.λπ.).
REM =====================================================================

cd /d "%~dp0"

REM Αν έχεις pythonw.exe διαθέσιμο (έρχεται μαζί με κάθε εγκατάσταση Python
REM στα Windows), τρέχει χωρίς κανένα παράθυρο. Αν για κάποιο λόγο δεν
REM βρεθεί, πέφτει πίσω στο κανονικό python.exe (θα φανεί ένα παράθυρο).
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "server.py"
) else (
    start "" python "server.py"
)
