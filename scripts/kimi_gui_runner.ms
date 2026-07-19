(
    local markerPath = @"C:\Users\user\Documents\kimi\workspace\3DSL\scripts\kimi_marker.txt"
    local f = createFile markerPath
    format "ms_start\n" to:f
    close f
    try (
        python.ExecuteFile @"C:\Users\user\Documents\kimi\workspace\3DSL\scripts\kimi_onbox_test.py"
        f = openFile markerPath mode:"a"
        format "python_done\n" to:f
        close f
    ) catch (
        f = openFile markerPath mode:"a"
        format ("python_error: " + (getCurrentException()) + "\n") to:f
        close f
    )
    f = openFile markerPath mode:"a"
    format "quitting\n" to:f
    close f
    quitMax #noPrompt
)
