from pydantic import BaseModel, EmailStr

class EmailRequest(BaseModel):
    para: EmailStr
    assunto: str
    mensagem: str