import os
from argparse import ArgumentParser, Namespace

from rich.progress import Progress, track

from ..console import console
from .cli import CLI, CLICommand


@CLI.register_command
class ARKParser(CLICommand):
    COMMAND = 'ark'
    HELP = 'Extract .ark files'
    
    @classmethod
    def build_args(cls, parser: ArgumentParser):
        parser.add_argument(
            'files',
            nargs = '+',
            help = 'input .ark files',
        )
        
        parser.add_argument(
            '-f', '--separate-folders',
            dest = 'separate_folders',
            help = 'Output each .ark file in separate folders',
            action = 'store_true',
        )
        
        parser.add_argument(
            '-o', '--output',
            dest = 'output',
            help = 'output directory for .ark file(s)',
        )
        
        parser.add_argument(
            '-i', '--ignore-errors',
            dest = 'ignore_errors',
            action = 'store_true',
            help = 'ignore errors',
        )
    
    @classmethod
    def run_command(cls, args: Namespace):
        import os
        from glob import glob

        from ..ark import ARK
            
        output = './'
        
        files = []
        for pattern in args.files:
            files.extend(glob(pattern))
        
        if args.output:
            output = args.output
        elif len(files) == 1:
            output = os.path.splitext(os.path.basename(args.files[0]))[0]
        
        def extract_all(ark_file: ARK, output: str):
            failed = []
            
            for file_metadata in track(
                ark_file.files,
                console = console,
                description = 'Extracting...',
            ):
                console.print(f'extracting: [yellow]{file_metadata.full_path}[/yellow]')
                try:
                    file = ark_file.read_file(file_metadata)
                    file.save(os.path.join(output, file_metadata.full_path))
                except Exception as e:
                    if args.ignore_errors:
                        failed.append(file_metadata.full_path)
                        console.print(f'[red]could not extract {file_metadata.full_path}[/red]')
                        continue
                    else:
                        e.add_note(f'file: {file_metadata.full_path}')
                        raise e
            
            return failed
        
        if len(files) == 1:
            with ARK(args.files[0]) as ark_file:
                failed = extract_all(ark_file, output)
        else:
            failed: dict[str, list[str]] = {}
            for filename in files:
                filename: str
                if args.separate_folders:
                    path = os.path.join(output, os.path.splitext(os.path.basename(filename))[0])
                else:
                    path = output
                
                
                with ARK(filename) as ark_file:
                    failed[filename] = extract_all(ark_file, path)
                
            if len(failed) > 0:
                for arkfile, files in failed.items():
                    for file in files:
                        console.print(f'[red]failed to extract {file} from {arkfile}')
                
                        
