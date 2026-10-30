#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

#define PATH_CAPACITY 32768

static int fail(const wchar_t *message) {
    MessageBoxW(NULL,message,L"AirFrame",MB_OK|MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance,HINSTANCE previous,PWSTR args,int show) {
    wchar_t directory[PATH_CAPACITY],python[PATH_CAPACITY],command[PATH_CAPACITY];
    wchar_t log_directory[PATH_CAPACITY],log_path[PATH_CAPACITY];
    DWORD length=GetModuleFileNameW(NULL,directory,PATH_CAPACITY);
    if(!length || length>=PATH_CAPACITY)
        return fail(L"Не удалось определить папку AirFrame.");
    wchar_t *last=wcsrchr(directory,L'\\');
    if(!last)return fail(L"Не удалось определить папку AirFrame.");
    *last=0;
    if(_snwprintf(python,PATH_CAPACITY,L"%ls\\python.exe",directory)<0 ||
       _snwprintf(command,PATH_CAPACITY,L"\"%ls\" -u -X utf8 -I \"%ls\\app-entry.py\"%ls%ls",
                  python,directory,args && *args ? L" " : L"",args ? args : L"")<0)
        return fail(L"Слишком длинный путь или параметры запуска AirFrame.");

    length=GetEnvironmentVariableW(L"LOCALAPPDATA",log_directory,PATH_CAPACITY);
    if(!length || length>=PATH_CAPACITY ||
       _snwprintf(log_path,PATH_CAPACITY,L"%ls\\AirFrame",log_directory)<0)
        return fail(L"Не удалось определить папку журнала AirFrame.");
    if(!CreateDirectoryW(log_path,NULL) && GetLastError()!=ERROR_ALREADY_EXISTS)
        return fail(L"Не удалось создать папку журнала AirFrame.");
    if(_snwprintf(log_path,PATH_CAPACITY,L"%ls\\AirFrame\\startup.log",log_directory)<0)
        return fail(L"Слишком длинный путь к журналу AirFrame.");

    SECURITY_ATTRIBUTES security={sizeof(security),NULL,TRUE};
    HANDLE log=CreateFileW(log_path,GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,
                          &security,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    HANDLE input=CreateFileW(L"NUL",GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,
                            &security,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    if(log==INVALID_HANDLE_VALUE || input==INVALID_HANDLE_VALUE) {
        if(log!=INVALID_HANDLE_VALUE)CloseHandle(log);
        if(input!=INVALID_HANDLE_VALUE)CloseHandle(input);
        return fail(L"Не удалось открыть журнал запуска AirFrame.");
    }
    STARTUPINFOW startup={0};startup.cb=sizeof(startup);
    startup.dwFlags=STARTF_USESTDHANDLES;
    startup.hStdOutput=log;startup.hStdError=log;startup.hStdInput=input;
    PROCESS_INFORMATION process={0};
    // python.exe with redirected handles records startup failures without a console window.
    BOOL started=CreateProcessW(python,command,NULL,NULL,TRUE,CREATE_NO_WINDOW,
                                NULL,directory,&startup,&process);
    CloseHandle(input);CloseHandle(log);
    if(!started)
        return fail(L"Не удалось запустить AirFrame. Распакуйте архив целиком.");
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess,INFINITE);
    DWORD code=1;GetExitCodeProcess(process.hProcess,&code);
    CloseHandle(process.hProcess);
    if(code) {
        wchar_t message[PATH_CAPACITY+256];
        _snwprintf(message,PATH_CAPACITY+256,
                   L"AirFrame завершился с ошибкой (код %lu).\nПодробности в журнале:\n%ls",
                   code,log_path);
        MessageBoxW(NULL,message,L"AirFrame",MB_OK|MB_ICONERROR);
    }
    return (int)code;
}
