with open(r'c:\Users\HP\Desktop\Files\GRID\calibration_lib\ec\processors\L2_processor.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('ax.set_ylabel("Counts")', 'ax.set_ylabel("counts")')

with open(r'c:\Users\HP\Desktop\Files\GRID\calibration_lib\ec\processors\L2_processor.py', 'w', encoding='utf-8') as f:
    f.write(text)
