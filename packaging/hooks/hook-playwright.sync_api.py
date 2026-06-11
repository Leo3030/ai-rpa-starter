from PyInstaller.utils.hooks import collect_data_files


datas = [
    item
    for item in collect_data_files("playwright")
    if ".local-browsers" not in item[0] and ".local-browsers" not in item[1]
]
