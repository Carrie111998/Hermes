#!/usr/bin/env python3
"""
批量翻译 Hermes Agent 文档
"""
import os
import shutil
from pathlib import Path

# 源目录和目标目录
SOURCE_DIR = Path("/Users/pander/Documents/hermes-agent/website/docs")
TARGET_DIR = Path("/Users/pander/Documents/hermes-agent/website/docs-cn")

def copy_structure():
    """复制目录结构"""
    for item in SOURCE_DIR.rglob('*'):
        if item.is_dir():
            target_path = TARGET_DIR / item.relative_to(SOURCE_DIR)
            target_path.mkdir(parents=True, exist_ok=True)
    
    # 复制 _category_.json 文件
    for category_file in SOURCE_DIR.rglob('_category_.json'):
        target_file = TARGET_DIR / category_file.relative_to(SOURCE_DIR)
        if not target_file.exists():
            shutil.copy2(category_file, target_file)
            print(f"已复制: {category_file.relative_to(SOURCE_DIR)}")

if __name__ == "__main__":
    print("开始复制目录结构...")
    copy_structure()
    print("目录结构复制完成！")
    print(f"\n请在以下目录继续手动翻译文档:")
    print(f"{TARGET_DIR}")
