from langchain_chroma.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

CAMINHO_DB = "db"


# prompt_template = f"""
# Responda a pergunta do usuario:
# {pergunta} 

# com base nessas informacoes: 
# {Base_conhecimento}

# Caso voce nao encontre a resposta do usuario baseado nas informacoes, informe que nao foi possivel responder.
# """

pergunta = input("Escreva sua pergunta: ")


funcao_embedding = OpenAIEmbeddings()
db = Chroma(persist_directory = CAMINHO_DB, embedding_function = funcao_embedding)

resultados =  db.similarity_search_with_relevance_scores(pergunta)

print(resultados)
print(len(resultados))