import axios from "axios";

const API_URL = "http://localhost:8000/cuentas";

export const getAccounts = async () => {
  const res = await axios.get(API_URL);
  return res.data;
};

export const createAccount = async (data) => {
  const res = await axios.post(API_URL, data);
  return res.data;
};

export const updateAccount = async (id, data) => {
  const res = await axios.put(`${API_URL}/${id}`, data);
  return res.data;
};

export const deleteAccount = async (id) => {
  await axios.delete(`${API_URL}/${id}`);
};
