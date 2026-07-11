; Script modificado para cambiar el nombre del instalador, agregar un icono y un archivo de instrucciones.
#define MyAppName "ExaStock"
#define MyAppVersion "1.2"
#define MyAppPublisher "AVQ"
#define MyAppExeName "ExaStock.exe"
#define MyAppAssocName MyAppName + " File"
#define MyAppAssocExt ".myp"
#define MyAppAssocKey StringChange(MyAppAssocName, " ", "") + MyAppAssocExt
#define MyAppIcon "C:\Users\PROPIETARIO\Desktop\ConteoInventario\ExaStock V1\ExacStock.ico"

; =========================================================================
; RUTA DE TU ARCHIVO DE INSTRUCCIONES
; =========================================================================
; Asegúrate de crear este archivo "instrucciones.txt" en esa misma carpeta.
#define MyAppReadme "C:\Users\PROPIETARIO\Desktop\ConteoInventario\ExaStock V1\Instrucciones.txt"
; =========================================================================

[Setup]
AppId={{48F6E505-CEDD-483E-84EE-0FEDA91C3E0D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest

; Muestra las instrucciones en una ventana dedicada antes de finalizar la instalación:

OutputBaseFilename=Instalador_ExaStock
SetupIconFile={#MyAppIcon}
SolidCompression=yes
WizardStyle=modern dynamic

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Ejecutable principal de la aplicación
Source: "C:\Users\PROPIETARIO\Desktop\ConteoInventario\ExaStock V1\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; COPIAR LA CARPETA STATIC (librerias JS para el escaner del celular):
Source: "C:\Users\PROPIETARIO\Desktop\ConteoInventario\ExaStock V1\static\*"; DestDir: "{app}\static"; Flags: ignoreversion recursesubdirs createallsubdirs

; COPIAR EL ARCHIVO DE INSTRUCCIONES A LA CARPETA DE INSTALACIÓN:
Source: "{#MyAppReadme}"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Registry]
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocExt}\OpenWithProgids"; ValueType: string; ValueName: "{#MyAppAssocKey}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}"; ValueType: string; ValueName: ""; ValueData: "{#MyAppAssocName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\{#MyAppExeName}"

; Acceso directo opcional en el Menú Inicio para abrir las instrucciones fácilmente:
Name: "{group}\Instrucciones de Uso"; Filename: "{app}\instrucciones.txt"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
