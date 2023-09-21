from db import *

def gen_shop_files(db):
    shop_files = []
    results = db.session.query(Files.id, Files.filename, Files.size).all()
    for f in results:
        db_id = f[0]
        filename = f[1]
        size = f[2]
        shop_files.append({
            "url": f'/api/get_game/{db_id}#{filename}',
            'size': size
        })
    return shop_files

def gen_shop(db, app_settings):
    shop_files = gen_shop_files(db)
    shop = {
        "files": shop_files,
        "success": app_settings['shop']['motd']
    }
    return shop