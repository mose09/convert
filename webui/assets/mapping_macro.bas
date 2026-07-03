Attribute VB_Name = "MappingExport"
' ==========================================================================
' AS-IS -> TO-BE 컬럼 매핑 9컬럼 시트를 convert-mapping 용 .md 로 내보낸다.
'
' 설치 (최초 1회):
'   1) 엑셀에서 Alt+F11 (Visual Basic 편집기)
'   2) 파일 > 파일 가져오기 > 이 mapping_macro.bas 선택
'   3) 통합문서를 "Excel 매크로 사용 통합문서(*.xlsm)" 로 저장
'   4) (선택) 시트에 버튼/도형을 그리고 마우스 우클릭 > 매크로 지정 >
'      ExportMappingMd 연결
'
' 사용:
'   "매핑" 시트(없으면 현재 시트)에 9개 열을 채운 뒤 ExportMappingMd 실행.
'   통합문서와 같은 폴더에 column_mapping.md 가 생성된다 (UTF-8, 한글 OK).
'   → 이 .md 를 convert-mapping / 마이그레이션 화면 입력으로 사용.
' ==========================================================================
Option Explicit

Sub ExportMappingMd()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets("매핑")
    On Error GoTo 0
    If ws Is Nothing Then Set ws = ActiveSheet

    Dim headers As Variant
    headers = Array("asis_table", "asis_column", "asis_column_type", _
        "tobe_table", "tobe_table_comment", "tobe_column", _
        "tobe_column_type", "tobe_column_comment", "remark")

    Dim md As String, i As Integer
    md = "| " & Join(headers, " | ") & " |" & vbLf
    Dim sep As String
    sep = "|"
    For i = 0 To UBound(headers)
        sep = sep & "------|"
    Next i
    md = md & sep & vbLf

    Dim r As Long, c As Integer, lastRow As Long, wrote As Long
    Dim rowVals() As String, val As String, isEmpty As Boolean
    lastRow = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    For r = 2 To lastRow
        ReDim rowVals(0 To UBound(headers))
        isEmpty = True
        For c = 0 To UBound(headers)
            val = Trim(CStr(ws.Cells(r, c + 1).Value))
            val = Replace(val, "|", "\|")           ' 파이프 이스케이프
            val = Replace(val, vbCr, " ")
            val = Replace(val, vbLf, " ")
            If Len(val) > 0 Then isEmpty = False
            rowVals(c) = val
        Next c
        If Not isEmpty Then
            md = md & "| " & Join(rowVals, " | ") & " |" & vbLf
            wrote = wrote + 1
        End If
    Next r

    Dim outPath As String
    If Len(ThisWorkbook.Path) > 0 Then
        outPath = ThisWorkbook.Path & "\column_mapping.md"
    Else
        outPath = Environ("USERPROFILE") & "\Desktop\column_mapping.md"
    End If

    ' UTF-8 로 저장 (VBA 기본 Print 는 ANSI 라 한글 깨짐 → ADODB.Stream)
    Dim stream As Object
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 2                 ' adTypeText
    stream.Charset = "utf-8"
    stream.Open
    stream.WriteText md
    stream.SaveToFile outPath, 2    ' adSaveCreateOverWrite
    stream.Close

    MsgBox wrote & " 행을 내보냈습니다:" & vbLf & outPath, vbInformation, _
        "매핑 MD 내보내기"
End Sub
