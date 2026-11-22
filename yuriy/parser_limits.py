"""Resource caps for the fixed extraction subprocess, not a filesystem sandbox."""
import sys

_JOB = None


def apply_limits():
    global _JOB
    memory=768*1024*1024
    if sys.platform!='win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
        resource.setrlimit(resource.RLIMIT_CPU,(25,25))
        return
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
    class Io(ctypes.Structure):
        _fields_=[(name,ctypes.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class Extended(ctypes.Structure):
        _fields_=[('BasicLimitInformation',Basic),('IoInfo',Io),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];k.CreateJobObjectW.restype=wintypes.HANDLE
    k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];k.SetInformationJobObject.restype=wintypes.BOOL
    k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE];k.AssignProcessToJobObject.restype=wintypes.BOOL
    k.GetCurrentProcess.argtypes=[];k.GetCurrentProcess.restype=wintypes.HANDLE
    k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
    handle=k.CreateJobObjectW(None,None)
    if not handle:raise ValueError('Windows не создала ограниченный процесс чтения. Извлечение отменено.')
    limit=Extended();limit.BasicLimitInformation.LimitFlags=0x100|0x2
    limit.BasicLimitInformation.PerProcessUserTimeLimit=25*10_000_000
    limit.ProcessMemoryLimit=memory
    if not k.SetInformationJobObject(handle,9,ctypes.byref(limit),ctypes.sizeof(limit)) or not k.AssignProcessToJobObject(handle,k.GetCurrentProcess()):
        k.CloseHandle(handle)
        raise ValueError('Windows не разрешила лимиты памяти/CPU парсера. Извлечение отменено, исходный файл не изменён.')
    _JOB=handle  # Keep the unnamed job alive until the parser exits.
