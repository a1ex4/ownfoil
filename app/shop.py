from db import *

def gen_shop_files(db):
    shop_files = []
    results = db.session.query(Files.id, Files.filename, Files.size, Files.app_id, Files.version, Files.extension).all()
    for f in results:
        db_id = f[0]
        filename = f[1]
        size = f[2]
        app_id = f[3]
        version = f[4]
        extension = f[5]
        display_name = filename
        if f'[{app_id}]' not in display_name:
            display_name = display_name.replace(f'.{extension}', '') + f' [{app_id}]' + f'.{extension}'
        
        if f'[v{version}]' not in display_name:
            display_name = display_name.replace(f'.{extension}', '') + f'[v{version}]' + f'.{extension}'
        shop_files.append({
            "url": f'/api/get_game/{db_id}#{display_name}',
            'size': size
        })
    return shop_files
