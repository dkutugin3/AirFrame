#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance,HINSTANCE previous,PWSTR args,int show) {
    wchar_t directory[32768],python[32768],command[65536];
    if(!GetModuleFileNameW(NULL,directory,32768))return 1;
    wchar_t *last=wcsrchr(directory,L'\\');
    if(!last)return 1;
    *last=0;
    _snwprintf(python,32768,L"%ls\\pythonw.exe",directory);
    _snwprintf(command,65536,L"\"%ls\" -I \"%ls\\app-entry.py\"",python,directory);
    STARTUPINFOW startup={0};startup.cb=sizeof(startup);
    PROCESS_INFORMATION process={0};
    if(!CreateProcessW(python,command,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,directory,&startup,&process)) {
        MessageBoxW(NULL,L"Не удалось запустить AirFrame. Распакуйте архив целиком.",L"AirFrame",MB_OK|MB_ICONERROR);
        return 1;
    }
    WaitForSingleObject(process.hProcess,INFINITE);
    DWORD code=0;GetExitCodeProcess(process.hProcess,&code);
    CloseHandle(process.hThread);CloseHandle(process.hProcess);
    return (int)code;
}
