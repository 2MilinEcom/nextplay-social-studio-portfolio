"""Launch V4 code against an existing workspace without copying or replacing it."""
import argparse
from pathlib import Path
from dotenv import load_dotenv
from pipeline_core.storage import load_product_workspace
from pipeline_core.dashboard import serve_dashboard

def main():
    parser=argparse.ArgumentParser(description='NextPlay 4 non-destructive timeline editor')
    parser.add_argument('--workspace',required=True,help='Existing product directory containing product.json')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    path=Path(args.workspace).expanduser().resolve()
    if not (path/'product.json').is_file():
        parser.error('Choose the product directory containing product.json. No files were changed.')
    # Reuse the existing local configuration in place; never copy secrets into this release.
    load_dotenv(path.parent.parent/'.env',override=False)
    product,workspace=load_product_workspace(path)
    serve_dashboard(product,workspace,port=args.port,open_browser=not args.no_browser)

if __name__=='__main__':main()
