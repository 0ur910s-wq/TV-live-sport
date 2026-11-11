' =====================================================================
'  Αυτόματος εντοπισμός φακέλου: βρίσκει μόνο του το Desktop του χρήστη
'  που τρέχει το script (shell.SpecialFolders), και μετά ψάχνει τον
'  υποφάκελο "tv" μέσα σε αυτό — δηλαδή Desktop\tv, όπως φαίνεται στο
'  File Explorer σου. Δεν χρειάζεται να γράψεις username ή δίσκο.
'
'  Αν ο φάκελος της εφαρμογής σου ΔΕΝ είναι Desktop\tv, άλλαξε μόνο τη
'  γραμμή appFolder παρακάτω με το σωστό path.
' =====================================================================

Set shell = CreateObject("WScript.Shell")
appFolder = shell.SpecialFolders("Desktop") & "\tv"

Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FolderExists(appFolder) Then
    MsgBox "Ο φάκελος '" & appFolder & "' δεν βρέθηκε." & vbCrLf & _
           "Άνοιξε το start_server_hidden.vbs με σημειωματάριο και διόρθωσε" & vbCrLf & _
           "τη γραμμή appFolder ώστε να δείχνει στον σωστό φάκελο.", _
           vbCritical, "Λάθος ρύθμιση φακέλου"
    WScript.Quit
End If

shell.CurrentDirectory = appFolder
shell.Run """" & appFolder & "\start_server.bat""", 0, False
