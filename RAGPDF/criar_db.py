from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

PASTA_BASE = "base"

def criar_db():
    documentos = carregar_documentos()
    chunks =  dividir_chunks(documentos)
    vetorizar_chunks(chunks)
    
def carregar_documentos():     
    carregador = PyPDFDirectoryLoader(PASTA_BASE, glob = "*.pdf")
    documentos=carregador.load()
    return documentos

def dividir_chunks(documentos):
    separador_documentos = RecursiveCharacterTextSplitter(
        chunk_size = 2000,
        chunk_overlap = 500,
        length_function = len,
        add_start_index=True,
        
    )
    chunks = separador_documentos.split_documents(documentos)
    return chunks   
    
def vetorizar_chunks(chuncks):
    db = Chroma.from_documents(chuncks, OpenAIEmbeddings(), persist_directory = "db")

criar_db()