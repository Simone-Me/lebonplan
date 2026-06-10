from pathlib import Path

path = Path('rewrite_eda_transport.py')
text = path.read_text(encoding='utf-8')
text = text.replace(r"print('\n", r"print('\\n")
text = text.replace(r"print(f'\n", r"print(f'\\n")
path.write_text(text, encoding='utf-8')
print('script fixed')
