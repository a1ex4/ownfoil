# Default configuration variables

# Files with these extensions will be added to the shop
valid_ext = ['nsp', 'nsz', 'xci']

# Shop template file to use
template_name = 'shop_template.jsonc'

# Default shop content if no template is provided
default_shop = {
    'files' : {},
    'directories': []
}

# Scan interval, in minutes
scan_interval = 5