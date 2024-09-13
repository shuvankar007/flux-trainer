import os
import json
import random
import toml
import subprocess
import logging
from typing import Dict, Any, List
from tqdm import tqdm
from datetime import datetime
import torch
import gc
from PIL import Image
import shutil

from pathlib import Path
def path_to_str(obj: Any) -> Any:
    """Convert Path objects to strings."""
    if isinstance(obj, Path):
        return str(obj)
    elif isinstance(obj, dict):
        return {k: path_to_str(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [path_to_str(v) for v in obj]
    return obj

def construct_toml(config: Dict[str, Any]) -> Dict[str, Any]:
    """Construct and update the TOML configuration file."""
    try:
        with open(config["dataset_toml"], 'r') as file:
            toml_data = toml.load(file)

        if 'datasets' in toml_data:
            for dataset in toml_data['datasets']:
                if 'subsets' in dataset:
                    for subset in dataset['subsets']:
                        subset['image_dir'] = config['dataset_path']
        logging.info(f"All instances of 'image_dir' in dataset.toml updated to: {config['dataset_path']}")
        
        toml_data['general']['flip_aug'] = config["mode"] != "face"
        if not toml_data['general']['flip_aug']:
            logging.info("Disabling flip augmentation for face mode.")

        toml_file_path = Path(config["output_dir"]) / "dataset.toml"
        with open(toml_file_path, 'w') as file:
            toml.dump(toml_data, file)
        
        config["dataset_config"] = str(toml_file_path)
    except Exception as e:
        logging.error(f"Error in construct_toml: {str(e)}")
        raise

    return config

def construct_config(config_path: str) -> Dict[str, Any]:
    """Construct and update the configuration dictionary."""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        config["timestamp"] = timestamp
        config["output_name"] = f"{Path(config['dataset_path']).name}_{timestamp}"
        config["output_dir"] = str(Path("results") / config['output_name'])

        Path(config["output_dir"]).mkdir(parents=True, exist_ok=True)
        
        # Convert all Path objects to strings before JSON serialization
        serializable_config = path_to_str(config)
        
        with open(Path(config["output_dir"]) / "config.json", 'w') as f:
            json.dump(serializable_config, f, indent=4)

        return construct_toml(config)
    except Exception as e:
        logging.error(f"Error in construct_config: {str(e)}")
        raise

def run_job(cmd: List[str], config: Dict[str, Any]) -> None:
    """Run the training job and log output."""
    log_file = Path(config["output_dir"]) / f"training_log_{config['timestamp']}.txt"
    try:
        with open(log_file, 'w') as f:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                print(line, end='')
                f.write(line)
                f.flush()

        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
    except Exception as e:
        logging.error(f"Error in run_job: {str(e)}")
        raise

#workaround for unnecessary flash_attn requirement
from unittest.mock import patch
from transformers.dynamic_module_utils import get_imports
from transformers import AutoProcessor, AutoModelForCausalLM 

def fixed_get_imports(filename: str | os.PathLike) -> list[str]:
    if not str(filename).endswith("modeling_florence2.py"):
        return get_imports(filename)
    imports = get_imports(filename)
    imports.remove("flash_attn")
    return imports

device = "cuda:0" if torch.cuda.is_available() else "cpu"
torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32

def get_image_and_caption_paths(dataset_dir):
    image_paths = []
    caption_paths = []

    # Walk through all subdirectories and files in dataset_dir
    for root, _, files in os.walk(dataset_dir):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                image_paths.append(os.path.join(root, file))
            elif file.lower().endswith('.txt'):
                caption_paths.append(os.path.join(root, file))

    # Sort the paths for consistency
    image_paths.sort()
    caption_paths.sort()

    return image_paths, caption_paths

@torch.no_grad()
def florence_caption_dataset(dataset_dir, 
        caption_mode="<CAPTION>",
        florence_model_path="./models",
        batch_size=1):

    os.makedirs(florence_model_path, exist_ok=True)
    image_paths, caption_paths = get_image_and_caption_paths(dataset_dir)

    print(f"Found {len(image_paths)} images and {len(caption_paths)} txt files.")
    if len(caption_paths):
        print(f"WARNING: This script will overwrite the existing txt files!!")
    print(f"Captioning {len(image_paths)} images...")

    with patch("transformers.dynamic_module_utils.get_imports", fixed_get_imports):
        model = AutoModelForCausalLM.from_pretrained("microsoft/Florence-2-large", attn_implementation="sdpa", device_map=device, torch_dtype=torch_dtype, trust_remote_code=True, cache_dir=florence_model_path)
            
    processor = AutoProcessor.from_pretrained("microsoft/Florence-2-large", trust_remote_code=True, cache_dir=florence_model_path)

    for i in tqdm(range(0, len(image_paths), batch_size)):
        batch_paths = image_paths[i:i+batch_size]
        batch_images = [Image.open(path).convert("RGB") for path in batch_paths]
        
        inputs = processor(text=[caption_mode] * len(batch_images), images=batch_images, return_tensors="pt", padding=True).to(device, torch_dtype)
        generated_ids = model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=1024,
            num_beams=4
        )

        generated_texts = processor.batch_decode(generated_ids, skip_special_tokens=False)
        parsed_answers = [processor.post_process_generation(text, task=caption_mode, image_size=(img.width, img.height)) for text, img in zip(generated_texts, batch_images)]
        
        for path, parsed_answer in zip(batch_paths, parsed_answers):
            caption = parsed_answer[caption_mode].replace("The image shows a ", "A ")
            caption = parsed_answer[caption_mode].replace("<pad>", "")
            basename = os.path.splitext(os.path.basename(path))[0]
            dirname  = os.path.dirname(path)
            with open(f"{os.path.join(dirname, basename)}.txt", "w") as f:
                f.write(caption)

        # Close images to free up memory
        for img in batch_images:
            img.close()

    model.to('cpu')
    del model
    del processor
    gc.collect()
    torch.cuda.empty_cache()

    return

def prep_dataset(root_directory, soft_clean = False):
    error_dir = os.path.join(os.path.dirname(root_directory), 'errors')
    os.makedirs(error_dir, exist_ok=True)

    print("Preparing dataset folder {root_directory}...")
    total_imgs, resized = 0, 0

    for subdir, _, files in os.walk(root_directory):
        for file in files:
            file_path = os.path.join(subdir, file)

            if soft_clean and file_path.lower().endswith(('.txt', '.npz')):
                continue

            try:
                # Try loading the file as an image and converting it to RGB
                with Image.open(file_path) as img:
                    img = img.convert("RGB")
                    
                    if max(img.width, img.height) > 2048:
                        # Resize the image with max width/height of 2048
                        img.thumbnail((2048, 2048), Image.LANCZOS)
                        resized += 1
                    
                    # Save the image as .jpg
                    new_filename = os.path.splitext(file)[0] + '.jpg'
                    new_file_path = os.path.join(subdir, new_filename)
                    img.save(new_file_path, 'JPEG', quality=95)
                    total_imgs += 1
                
                # Delete the original file if it was different from the new one:
                if new_file_path != file_path:
                    os.remove(file_path)

            except Exception as e:
                # If there was any error, move the file to the errors directory
                print(f"Error preparing img: {e}")
                shutil.move(file_path, os.path.join(error_dir, file))

    print(f"{total_imgs} imgs in {root_directory} converted to .jpg Resized {resized} images.")

    files = os.listdir(root_directory)
    print(files)


if __name__ == "__main__":
    folder_path = "/data/xander/Projects/cog/GitHub_repos/flux-trainer/test"
    caption_mode = "<CAPTION>"
    batch_size = 1
    florence_caption_dataset(folder_path, batch_size=batch_size, caption_mode = caption_mode)