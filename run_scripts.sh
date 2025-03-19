export GOOGLE_API_KEY="AIzaSyDw1rKH7uCHbOoJ_0kVwiyT49yWPHwh8l8"
export TORCH_DEVICE="cpu"

 python convert_single.py $1 --output_dir ./ --output_format markdown --use_llm --page_range "0-10" --gemini_api_key $GOOGLE_API_KEY 