import contextlib
import csv
import io
import os
import pathlib

from PIL import Image

from .file_utils import is_binary_file, is_text_file, PathOrFile
from .utils import posix_path, strToInt
from .pvr import PVR


class TexAtlas():
    def __init__(
        self,
        file: PathOrFile,
        search_folders: list[str] | None = None,
        smart_search: bool = True,
    ) -> None:
        self.filename = ''
        self.images = []
        
        if isinstance(file, str) and os.path.isfile(file):
            self.filename = file
            context_manager = open(file, 'r', newline = '')
        elif isinstance(file, (bytes, bytearray)):
            context_manager = io.BytesIO(file)
        elif isinstance(file, io.IOBase):
            context_manager = contextlib.nullcontext(file)
        else:
            context_manager = file
        
        with context_manager as csvfile:
            self.image_info = list({
                    **line,
                    'x': strToInt(line.get('x', 0)),
                    'y': strToInt(line.get('y', 0)),
                    'width': strToInt(line.get('width', 0)),
                    'height': strToInt(line.get('height', 0)),
                } for line in csv.DictReader(
                    csvfile,
                    ['filename', 'atlas', 'x', 'y', 'width', 'height'],
                    delimiter = '\t',
                )
            )
        
        if search_folders == None:
            search_folders = ['.']
        
        self.search_folders = search_folders
        self.smart_search = smart_search
        
        self.get_images()
    
    def get_images(self):
        self.images: list[Texture] = []
        
        atlas_file = ''
        atlas_image = None
        
        for image_data in self.image_info:
            new_atlas_file = self.find_file(image_data['atlas'])
            
            if (not atlas_file) or (not os.path.samefile(
                atlas_file,
                new_atlas_file,
            )):
                atlas_file = new_atlas_file
                if atlas_file.endswith('.pvr'):
                    atlas_image = PVR(atlas_file).image
                else:
                    atlas_image = Image.open(atlas_file)
            
            self.images.append(
                Texture(
                    image_data['filename'],
                    image_data['atlas'],
                    atlas_image.crop((
                        image_data['x'],
                        image_data['y'],
                        image_data['x'] + image_data['width'] - 1,
                        image_data['y'] + image_data['height'] - 1,
                    )),
                    dir = posix_path(os.path.dirname(atlas_file))
                )
            )
            
    def get_image(self, image_data: dict[str, str | int]):
        atlas_file = self.find_file(image_data['atlas'])
        
        atlas_image = Image.open(atlas_file)
        
        return Texture(
            image_data['filename'],
            image_data['atlas'],
            atlas_image.crop((
                image_data['x'],
                image_data['y'],
                image_data['x'] + image_data['width'] - 1,
                image_data['y'] + image_data['height'] - 1,
            )),
            dir = posix_path(atlas_file).removesuffix('/' + posix_path(image_data['atlas']))
        )
    
    def find_file(self, path: str):
        pvr_name = os.path.splitext(path)[0] + '.pvr'
        for dir in self.search_folders:
            if os.path.isfile(filename := os.path.join(dir, path)):
                return filename
            elif os.path.isfile(filename := os.path.join(dir, pvr_name)):
                return filename
        
        if self.smart_search and self.filename:
            path_obj = pathlib.Path(path)
            pvr_obj = pathlib.Path(pvr_name)
            filename_path = pathlib.Path(os.path.dirname(self.filename)).absolute()
            
            while len(filename_path.parts) > 1:
                if os.path.exists(filename := os.path.join(filename_path, path_obj)):
                    return filename
                elif os.path.exists(filename := os.path.join(filename_path, pvr_obj)):
                    return filename
                
                filename_path = pathlib.Path(*filename_path.parts[:-1])
            
        raise FileNotFoundError(f'File "{path}" was not found. Try different search folders.')

class Texture():
    def __init__(
        self,
        filename: str,
        atlas_path: str,
        image: Image.Image | str,
        dir: str = '.',
    ) -> None:
        self.filename = filename
        self.atlas_path = atlas_path

        if not isinstance(image, Image.Image):
            image = Image.open(image)
        self.image = image
        
        self.dir = dir
        
