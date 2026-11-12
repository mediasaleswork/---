"""Upload intact candidate evidence into the already configured private folder."""
import hashlib
import re
from urllib.parse import urlsplit


REPORT_RESERVE_BYTES = 256 * 1024**2


class ReportStorageReserve(Exception):
    def __init__(self, remaining):
        self.pause_all = remaining <= REPORT_RESERVE_BYTES
        super().__init__('Drive: место зарезервировано для таблицы и снимков')


def verify_private_folder(reporter):
    meta=reporter._json('GET','https://www.googleapis.com/drive/v3/files/'+reporter.drive_folder_id,
                        params={'fields':'mimeType,driveId,trashed,permissions(type),capabilities(canAddChildren)','supportsAllDrives':'true'})
    if (meta.get('trashed') or meta.get('mimeType')!='application/vnd.google-apps.folder'
        or not meta.get('capabilities',{}).get('canAddChildren') or not meta.get('permissions')
        or any(p.get('type')=='anyone' for p in meta['permissions'])):
        raise ValueError('Для вложений требуется закрытая папка архива')
    reporter.in_shared_drive = bool(meta.get('driveId'))


def upload_file(reporter,path,row):
    tag=hashlib.sha256((row['key']+':'+row['md5']).encode()).hexdigest()
    fields='id,md5Checksum,size'
    def verified(file):
        return (re.fullmatch(r'[\w-]+',str(file.get('id',''))) and
                file.get('md5Checksum')==row['md5'] and str(file.get('size'))==str(row['size']))
    found=reporter._json('GET','https://www.googleapis.com/drive/v3/files',params={
        'q':f"'{reporter.drive_folder_id}' in parents and trashed=false and appProperties has {{key='msbrb_candidate' and value='{tag}'}}",
        'fields':f'files({fields})','supportsAllDrives':'true','includeItemsFromAllDrives':'true'})
    for file in found.get('files',[]):
        if verified(file):return file['id']
    if not path.is_file() or path.stat().st_size!=row['size']:raise OSError('Нет проверенной локальной копии')
    if not getattr(reporter, 'in_shared_drive', False):
        quota = reporter._json('GET', 'https://www.googleapis.com/drive/v3/about',
                               params={'fields': 'storageQuota(limit,usage)'})['storageQuota']
        if 'limit' in quota:
            remaining = int(quota['limit']) - int(quota['usage'])
            if remaining - row['size'] < REPORT_RESERVE_BYTES:
                raise ReportStorageReserve(remaining)
    metadata={'name':row['name'],'mimeType':row['mime'],'parents':[reporter.drive_folder_id],
              'appProperties':{'msbrb_candidate':tag}}
    start=reporter.session.request('POST','https://www.googleapis.com/upload/drive/v3/files',
        params={'uploadType':'resumable','supportsAllDrives':'true','fields':fields},json=metadata,
        headers={'X-Upload-Content-Type':row['mime'],'X-Upload-Content-Length':str(row['size'])},timeout=60)
    start.raise_for_status()
    location=start.headers.get('Location','')
    dest=urlsplit(location)
    if dest.scheme!='https' or not (dest.hostname or '').endswith('.googleapis.com') or dest.username or dest.password:
        raise ValueError('Неверный адрес загрузки Google Drive')
    with path.open('rb') as f:
        result=reporter.session.request('PUT',location,data=f,headers={'Content-Type':row['mime'],
            'Content-Length':str(row['size'])},timeout=(20,600),allow_redirects=False)
    result.raise_for_status()
    file=result.json()
    if not verified(file):raise ValueError('Drive не подтвердил размер и контрольную сумму файла')
    return file['id']
